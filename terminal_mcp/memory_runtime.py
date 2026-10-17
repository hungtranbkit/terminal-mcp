"""Opt-in runtime: reuse enrolled node clients; no discovery implies trust."""
from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

from .memory_projection import project_sources
from .memory_sync import MemoryEndpoint, synchronize
from .unified_memory import MemoryStore, MemoryError, Principal

CONFIG_ENV='TERMINAL_MCP_MEMORY_CONFIG'


class MemoryRuntime:
    def __init__(self, config, *, controller=None, terminal=None):
        self.config=config;self.controller=controller;self.terminal=terminal
        self.store=MemoryStore(config['database'],config['node'])
        self.projects=config.get('projects',{})
        self.actor=Principal(self.store.node,frozenset({'reader','writer'}),frozenset(self.projects))
        self.endpoint=MemoryEndpoint(self.store,config['controller'],set(config.get('share_projects',[])))
        self._stop=threading.Event();self._thread=None;self._cycle_lock=threading.Lock()
        self._status={'state':'NOT_STARTED','peers':[]}

    @classmethod
    def load(cls, path, *, node, controller=None, terminal=None):
        with Path(path).open('rb') as f:raw=f.read(1048577)
        if len(raw)>1048576:raise MemoryError('CONFIG_TOO_LARGE')
        config=json.loads(raw)
        allowed={'node','controller','database','projects','share_projects','peers','session_database','interval_seconds'}
        if not isinstance(config,dict) or set(config)-allowed or config.get('node')!=node:raise MemoryError('INVALID_CONFIG_NODE')
        if not isinstance(config.get('controller'),str) or not config['controller'] or not isinstance(config.get('database'),str) or not Path(config['database']).is_absolute():raise MemoryError('INVALID_CONFIG')
        projects=config.get('projects',{})
        if not isinstance(projects,dict) or len(projects)>128 or not all(isinstance(k,str) and isinstance(v,dict) for k,v in projects.items()):raise MemoryError('INVALID_PROJECTS')
        if not isinstance(config.get('share_projects',[]),list) or not set(config.get('share_projects',[]))<=set(projects):raise MemoryError('INVALID_SHARE_SCOPE')
        peers=config.get('peers',{})
        if not isinstance(peers,dict) or len(peers)>64 or any(not isinstance(v,list) or not set(v)<=set(projects) for v in peers.values()):raise MemoryError('INVALID_PEERS')
        if peers and node!=config['controller']:raise MemoryError('CONTROLLER_ONLY')
        interval=config.get('interval_seconds',60)
        if not isinstance(interval,int) or not 10<=interval<=300:raise MemoryError('INVALID_INTERVAL')
        return cls(config,controller=controller,terminal=terminal)

    def transport(self, peer, action, body):
        if self.controller is None:raise MemoryError('NEEDS_AUTH')
        client=self.controller._clients.get(peer)
        if client is None or not hasattr(client,'memory_request'):raise MemoryError('NEEDS_AUTH')
        return client.memory_request(action,body)

    def tick(self):
        with self._cycle_lock:
            projection=project_sources(self.store,self.projects,shared_projects=self.config.get('share_projects',[]),session_db=self.config.get('session_database'),session_authorized=self.terminal._read_authorized if self.terminal else None)
            peers=[]
            for peer,scope in self.config.get('peers',{}).items():
                try:peers.append(synchronize(self.store,peer,set(scope),self.transport))
                except Exception as exc:
                    code=str(exc) if isinstance(exc,MemoryError) else 'PEER_UNAVAILABLE_OR_UNSUPPORTED'
                    peers.append({'peer':peer,'state':code,'cached_data_preserved':True})
            self._status={'state':'DEGRADED' if projection['errors'] or any(p['state']!='CURRENT' for p in peers) else 'CURRENT','projection':projection,'peers':peers}
            return self.status()

    def status(self):
        return {'node':self.store.node,'epoch':self.store.epoch,'version':1,**self._status}

    def search(self, query, *, project=None, limit=20):
        actor=self.actor
        if project is not None:
            actor.require('reader',project)
            actor=Principal(actor.node,actor.roles,frozenset({project}))
        return {'results':self.store.search(actor,query,limit=limit),'untrusted_output':True,'runtime':self.status()}

    def start(self):
        def run():
            while not self._stop.is_set():
                try:self.tick()
                except Exception:self._status={'state':'LOCAL_REFRESH_FAILED','peers':[]}
                self._stop.wait(self.config.get('interval_seconds',60))
        self._thread=threading.Thread(target=run,name='terminal-memory',daemon=True);self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:self._thread.join(timeout=10)
        if not self._thread or not self._thread.is_alive():self.store.close()


def from_environment(node, *, controller=None, terminal=None):
    path=os.environ.get(CONFIG_ENV)
    return MemoryRuntime.load(path,node=node,controller=controller,terminal=terminal) if path else None


def main(argv=None):
    parser=argparse.ArgumentParser(description='Local trusted operator access to opt-in Unified Memory')
    parser.add_argument('--config',required=True);parser.add_argument('--node',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('refresh');sub.add_parser('status')
    search=sub.add_parser('search');search.add_argument('query');search.add_argument('--project');search.add_argument('--limit',type=int,default=20)
    args=parser.parse_args(argv)
    runtime=None
    try:
        runtime=MemoryRuntime.load(args.config,node=args.node)
        result=runtime.search(args.query,project=args.project,limit=args.limit) if args.command=='search' else runtime.tick() if args.command=='refresh' else runtime.status()
        print(json.dumps(result,ensure_ascii=False));return 0
    except (MemoryError,OSError,ValueError):
        print(json.dumps({'error':'MEMORY_OPERATION_REFUSED'}));return 1
    finally:
        if runtime:runtime.close()


if __name__=='__main__':raise SystemExit(main())
