"""Versioned opt-in memory protocol using node-agent authentication.

The node's existing bearer authenticates its configured controller, never a
caller-supplied principal. Controller pulls only origin-owned records and is the
only relay. Discovery advertises capability, not permission.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .unified_memory import MemoryError, MemoryStore, Principal, VERSION, OFFLINE_TTL, MAX_PAGE_BYTES


@dataclass
class MemoryEndpoint:
    store: MemoryStore
    controller: str
    projects: set[str]

    @property
    def actor(self):
        return Principal(self.controller,frozenset({'reader','relay'}),frozenset(self.projects))

    def call(self, action: str, body: dict) -> dict:
        if not isinstance(body,dict):raise MemoryError('INVALID_REQUEST')
        allowed={'join':{'version','node'},'pull':{'cursor','epoch','projects'},'push':{'page'},'search':{'query','limit'}}
        if action not in allowed or set(body)-allowed[action]:raise MemoryError('INVALID_REQUEST')
        if action=='join':
            if body.get('version')!=VERSION:raise MemoryError('UNSUPPORTED_SCHEMA')
            if body.get('node')!=self.controller:raise MemoryError('BLOCKED_PERMISSION')
            return {'version':VERSION,'node':self.store.node,'epoch':self.store.epoch,
                    'state':'ENROLLED','projects':sorted(self.projects),'received':self.store.cursor(self.controller)}
        if action=='pull':
            requested=body.get('projects',sorted(self.projects))
            if not isinstance(requested,list) or not all(isinstance(p,str) for p in requested) or not set(requested)<=self.projects:
                raise MemoryError('BLOCKED_PERMISSION')
            actor=Principal(self.controller,self.actor.roles,frozenset(requested))
            return self.store.page(actor,body.get('cursor',0),epoch=body.get('epoch'),owned_only=True)
        if action=='push':
            page=body.get('page')
            if not isinstance(page,dict) or page.get('node')!=self.controller:raise MemoryError('PEER_ID_MISMATCH')
            current=self.store.cursor(self.controller)
            if current['epoch'] and current['epoch']!=page.get('epoch'):
                # Epoch recovery resets bookkeeping only; history is never erased.
                self.store.reset_cursor(self.controller,page.get('epoch'))
            return self.store.receive(self.actor,page,peer_id=self.controller)
        limit=body.get('limit',20)
        if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=50:raise MemoryError('INVALID_QUERY')
        return {'results':self.store.search(self.actor,body.get('query',''),limit=limit),'untrusted_output':True}


def synchronize(store: MemoryStore, peer: str, projects: set[str], transport, *, max_pages=100) -> dict:
    """One bounded controller cycle; failures leave acknowledged cursors intact.

    Periodic bounded snapshots renew absolute offline leases, while ordinary
    cycles send deltas only. No deletion, no credential transfer, no live actions.
    """
    joined=transport(peer,'join',{'version':VERSION,'node':store.node})
    if joined.get('version')!=VERSION or joined.get('node')!=peer:raise MemoryError('JOIN_IDENTITY_MISMATCH')
    scope=frozenset(projects)&frozenset(joined['projects'])
    incoming=Principal(peer,frozenset({'writer'}),scope)
    outgoing=Principal(peer,frozenset({'reader'}),scope)
    policy=hashlib.sha256(json.dumps(sorted(scope)).encode()).hexdigest()
    refresh_key='refresh:'+peer+':'+policy+':'+joined['epoch']
    progress_key=refresh_key+':progress'
    with store.lock:
        row=store.db.execute('SELECT value FROM memory_meta WHERE key=?',(refresh_key,)).fetchone()
    now=store.clock();last=float(row[0]) if row else 0
    refresh=not row or now<last or now-last>=OFFLINE_TTL/2
    cursor=store.cursor(peer)
    if cursor['epoch']!=joined['epoch']:
        store.reset_cursor(peer,joined['epoch']);cursor=store.cursor(peer);refresh=True
    with store.lock:
        progress_row=store.db.execute('SELECT value FROM memory_meta WHERE key=?',(progress_key,)).fetchone()
    progress=json.loads(progress_row[0]) if progress_row else {}
    pull=progress.get('pull',0) if refresh else cursor['seq'];pulled=pushed=0;complete=True
    def save_progress():
        with store.lock,store.db:
            store.db.execute('INSERT OR REPLACE INTO memory_meta VALUES (?,?)',(progress_key,json.dumps(progress)))
    for _ in range(max_pages):
        page=transport(peer,'pull',{'cursor':pull,'epoch':joined['epoch'],'projects':sorted(scope)})
        if page.get('epoch')!=joined['epoch']:raise MemoryError('JOIN_EPOCH_CHANGED')
        receipt=store.receive(incoming,page,peer_id=peer)
        pulled+=receipt['accepted'];pull=page['cursor']
        if refresh:progress['pull']=pull;save_progress()
        if not page['more']:break
    else:complete=False
    remote=joined['received']
    push=progress.get('push',0) if refresh else (0 if remote['epoch']!=store.epoch else remote['seq'])
    for _ in range(max_pages):
        page=store.page(outgoing,push,exclude_owner=peer)
        receipt=transport(peer,'push',{'page':page})
        ack=receipt.get('cursor',{})
        if ack.get('epoch')!=store.epoch or ack.get('seq',-1)<page['cursor']:raise MemoryError('INVALID_ACK')
        pushed+=receipt['accepted'];push=page['cursor']
        if refresh:progress['push']=push;save_progress()
        if not page['more']:break
    else:complete=False
    if complete:
        with store.lock,store.db:
            store.db.execute('INSERT OR REPLACE INTO memory_meta VALUES (?,?)',(refresh_key,str(now if refresh else last)))
            store.db.execute('DELETE FROM memory_meta WHERE key=?',(progress_key,))
    return {'peer':peer,'state':'CURRENT' if complete else 'SYNCING','pulled':pulled,'pushed':pushed,'projects':sorted(scope)}


def memory_routes(endpoint: MemoryEndpoint, require_auth):
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def dispatch(request):
        if (blocked:=require_auth(request)) is not None:return blocked
        try:
            raw=bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw)>MAX_PAGE_BYTES+16384:raise MemoryError('REQUEST_TOO_LARGE')
            body=json.loads(raw)
            # SQLite operations run off the ASGI event loop, serialized by store lock.
            import anyio
            result=await anyio.to_thread.run_sync(lambda:endpoint.call(request.path_params['action'],body))
            return JSONResponse(result)
        except MemoryError as exc:
            return JSONResponse({'error':str(exc)},status_code=403 if str(exc)=='BLOCKED_PERMISSION' else 400)
        except (ValueError,TypeError,KeyError,OverflowError):
            return JSONResponse({'error':'INVALID_REQUEST'},status_code=400)
    return [Route('/v1/memory/{action}',dispatch,methods=['POST'])]
