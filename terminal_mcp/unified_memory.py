"""Additive, permission-scoped memory projection; never an execution authority.

Only sanitized projections enter this database. Source stores remain authoritative.
The append-only journal and acknowledgement cursors commit with the current view.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .redaction import redact_output

VERSION = 1
MAX_RECORD_BYTES = 32768
MAX_PAGE_BYTES = 524288
MAX_PAGE = 200
OFFLINE_TTL = 900
FIELDS = {
    'node': {'title','platform','status'},
    'repo': {'title','remote','path','head','branch','dirty'},
    'branch': {'title','head','repo_id'},
    'worktree': {'title','path','head','branch','repo_id','dirty'},
    'project': {'title','status','repo_id'},
    'service': {'title','status','project_id','endpoint'},
    'skill': {'title','version','digest','path','provider'},
    'task': {'title','status','priority','session','branch'},
    'session': {'title','status','agent_type','branch','head'},
    'note': {'title','body','tags','source','commit'},
    'checkpoint': {'title','body','tags','source','commit','status'},
}


class MemoryError(ValueError):
    """Errors contain fixed codes, never untrusted records or secret values."""


@dataclass(frozen=True)
class Principal:
    node: str
    roles: frozenset[str]
    projects: frozenset[str]

    def require(self, role: str, project: str | None = None) -> None:
        if role not in self.roles or (project is not None and project not in self.projects):
            raise MemoryError('BLOCKED_PERMISSION')


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(record: dict) -> str:
    return hashlib.sha256(canonical({k:v for k,v in record.items() if k not in ('hash','lease_until')}).encode()).hexdigest()


def identifier(value: Any) -> str:
    if not isinstance(value,str) or not value or len(value)>256 or any(ord(c)<32 for c in value):
        raise MemoryError('INVALID_ID')
    return value


def sanitize(kind: str, payload: dict) -> dict:
    if kind not in FIELDS or not isinstance(payload,dict) or set(payload)-FIELDS[kind]:
        raise MemoryError('INVALID_FIELDS')
    if len(canonical(payload).encode()) > MAX_RECORD_BYTES:
        raise MemoryError('RECORD_TOO_LARGE')
    result = {}
    for key,value in payload.items():
        if isinstance(value,str):
            if 'PRIVATE KEY-----' in value:
                raise MemoryError('CREDENTIAL_MATERIAL_REFUSED')
            if key in ('remote','endpoint') and '://' in value:
                try:
                    parsed=urlsplit(value)
                    if parsed.scheme not in ('https','http','ssh','git') or not parsed.hostname:
                        raise ValueError()
                    host=parsed.hostname
                    if ':' in host: host='['+host+']'
                    value=urlunsplit((parsed.scheme,host+(f':{parsed.port}' if parsed.port else ''),parsed.path,'',''))
                except ValueError:
                    raise MemoryError('INVALID_URL') from None
            value,_=redact_output(value,ansi_safe=True)
        elif isinstance(value,list) and key=='tags' and len(value)<=32 and all(isinstance(v,str) and len(v)<=128 for v in value):
            value=[redact_output(v,ansi_safe=True)[0] for v in value]
        elif value is not None and not isinstance(value,(bool,int)):
            raise MemoryError('INVALID_VALUE')
        result[key]=value
    return result


class MemoryStore:
    def __init__(self, path: str | Path, node: str, *, clock=None):
        self.node=identifier(node)
        if '/' in node: raise MemoryError('INVALID_NODE')
        self.clock=clock or time.time
        self.lock=threading.RLock()
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        try:
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        except FileExistsError:pass
        path.chmod(0o600)
        self.db=sqlite3.connect(path,check_same_thread=False,timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        with self.db:
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS memory_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS journal(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL,revision INTEGER NOT NULL,project TEXT NOT NULL,shareable INTEGER NOT NULL,record TEXT NOT NULL,UNIQUE(id,revision));
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,owner TEXT NOT NULL,kind TEXT NOT NULL,project TEXT NOT NULL,revision INTEGER NOT NULL,visibility TEXT NOT NULL,ever_shared INTEGER NOT NULL,record TEXT NOT NULL,search_text TEXT NOT NULL,received_at REAL NOT NULL,lease_until REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS memory_project ON documents(project,visibility);
            CREATE INDEX IF NOT EXISTS memory_delta ON journal(project,seq);
            CREATE TABLE IF NOT EXISTS projected(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS cursors(peer TEXT PRIMARY KEY,epoch TEXT NOT NULL,seq INTEGER NOT NULL,last_ok REAL NOT NULL);
            ''')
            for key,val in (('epoch',uuid.uuid4().hex),('node',node),('version',str(VERSION))):
                self.db.execute('INSERT OR IGNORE INTO memory_meta VALUES (?,?)',(key,val))
            if self.db.execute("SELECT value FROM memory_meta WHERE key='node'").fetchone()[0]!=node:
                raise MemoryError('NODE_ID_MISMATCH')
            if self.db.execute("SELECT value FROM memory_meta WHERE key='version'").fetchone()[0]!=str(VERSION):
                raise MemoryError('UNSUPPORTED_SCHEMA')
        self.epoch=self.db.execute("SELECT value FROM memory_meta WHERE key='epoch'").fetchone()[0]
        try:
            with self.db:
                self.db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(id UNINDEXED,text)')
            self.fts=True
        except sqlite3.OperationalError:
            self.fts=False

    def _write(self, obj: dict, *, lease: float) -> bool:
        old=self.db.execute('SELECT * FROM documents WHERE id=?',(obj['id'],)).fetchone()
        if old:
            prior=json.loads(old['record'])
            if prior['owner']!=obj['owner']: raise MemoryError('OWNER_CONFLICT')
            if prior['project']!=obj['project']: raise MemoryError('PROJECT_CONFLICT')
            historical=self.db.execute('SELECT record FROM journal WHERE id=? AND revision=?',(obj['id'],obj['revision'])).fetchone()
            if historical and json.loads(historical[0])['hash']!=obj['hash']: raise MemoryError('REVISION_CONFLICT')
            if obj['revision']<=old['revision']:
                # Fresh authenticated snapshots may renew only to their signed-in-transport
                # absolute deadline; replaying an old page cannot keep data alive.
                if obj['revision']==old['revision']:
                    if self.clock()<old['received_at']:
                        return False  # preserve rollback watermark; replay cannot reactivate cache
                    self.db.execute('UPDATE documents SET lease_until=MAX(lease_until,?),received_at=? WHERE id=?',(lease,self.clock(),obj['id']))
                return False
        shared=int(obj['visibility']=='shared' or bool(old and old['ever_shared']))
        raw=canonical(obj);now=self.clock()
        self.db.execute('INSERT INTO journal(id,revision,project,shareable,record) VALUES (?,?,?,?,?)',(obj['id'],obj['revision'],obj['project'],shared,raw))
        search=' '.join(str(v) for v in obj['payload'].values())
        self.db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)',(obj['id'],obj['owner'],obj['kind'],obj['project'],obj['revision'],obj['visibility'],shared,raw,search,now,lease))
        if self.fts:
            self.db.execute('DELETE FROM memory_fts WHERE id=?',(obj['id'],))
            self.db.execute('INSERT INTO memory_fts VALUES (?,?)',(obj['id'],search))
        return True

    def publish(self, actor: Principal, kind: str, local_id: str, project: str, payload: dict, *, visibility='private', projected=False) -> dict:
        actor.require('writer',project)
        if actor.node!=self.node: raise MemoryError('OWNER_CONFLICT')
        identifier(project);identifier(local_id)
        if visibility not in ('private','shared'): raise MemoryError('INVALID_VISIBILITY')
        obj={'id':f'{self.node}/{kind}/{self.epoch}/{local_id}','owner':self.node,'kind':kind,'project':project,'revision':1,'visibility':visibility,'payload':sanitize(kind,payload)}
        identifier(obj['id'])
        with self.lock,self.db:
            if projected:self.db.execute('INSERT OR IGNORE INTO projected VALUES (?)',(obj['id'],))
            row=self.db.execute('SELECT record FROM documents WHERE id=?',(obj['id'],)).fetchone()
            if row:
                prior=json.loads(row[0])
                if prior['project']!=project: raise MemoryError('PROJECT_CONFLICT')
                obj['revision']=prior['revision']
                if digest(obj)==prior['hash']: return prior
                obj['revision']+=1
            obj['hash']=digest(obj)
            self._write(obj,lease=0)
        return obj

    def reconcile_projection(self, seen: set[str]) -> None:
        """Withdraw only projector-managed records; keep sources and history intact."""
        with self.lock, self.db:
            for row in self.db.execute('SELECT d.record FROM projected p JOIN documents d ON d.id=p.id').fetchall():
                obj=json.loads(row[0])
                if obj['id'] in seen or obj['owner']!=self.node:
                    continue
                if obj['visibility']=='private' and not obj['payload']:
                    continue
                obj.update(revision=obj['revision']+1,visibility='private',payload={})
                obj['hash']=digest(obj)
                self._write(obj,lease=0)
            self.db.executemany('INSERT OR IGNORE INTO projected VALUES (?)',[(identity,) for identity in seen])

    def page(self, actor: Principal, cursor: int, *, epoch=None, limit=MAX_PAGE, owned_only=False, exclude_owner=None) -> dict:
        actor.require('reader')
        if not isinstance(cursor,int) or isinstance(cursor,bool) or cursor<0 or not 1<=limit<=MAX_PAGE:
            raise MemoryError('INVALID_CURSOR')
        with self.lock:
            high=self.db.execute('SELECT COALESCE(MAX(seq),0) FROM journal').fetchone()[0]
            if (epoch and epoch!=self.epoch) or cursor>high: raise MemoryError('CURSOR_RESET_REQUIRED')
            projects=sorted(actor.projects)
            sql='SELECT j.* FROM journal j JOIN documents d ON d.id=j.id WHERE j.seq>? AND j.shareable=1 AND j.project IN ('+','.join('?' for _ in projects)+')'
            args=[cursor,*projects]
            if owned_only:sql+=' AND d.owner=?';args.append(self.node)
            if exclude_owner:sql+=' AND d.owner!=?';args.append(exclude_owner)
            rows=self.db.execute(sql+' ORDER BY j.seq LIMIT ?', [*args,limit+1]).fetchall()
            objects=[];size=0;end=cursor;more=False;now=self.clock()
            for index,row in enumerate(rows):
                if index>=limit:more=True;break
                obj=json.loads(row['record'])
                current=self.db.execute('SELECT visibility,received_at,lease_until,owner FROM documents WHERE id=?',(obj['id'],)).fetchone()
                # A later privacy change must suppress even historical shared bodies.
                if obj['visibility']=='shared' and current['visibility']!='shared':
                    end=row['seq'];continue
                if obj['visibility']!='shared':
                    obj={**obj,'visibility':'private','payload':{}}
                    obj['hash']=digest(obj)
                if current['owner']!=self.node and not current['received_at']<=now<current['lease_until']:
                    end=row['seq'];continue
                obj['lease_until']=now+OFFLINE_TTL if current['owner']==self.node else current['lease_until']
                encoded=len(canonical(obj).encode())
                if size+encoded>MAX_PAGE_BYTES:more=True;break
                objects.append(obj);size+=encoded;end=row['seq']
            if not more:end=high
            return {'version':VERSION,'node':self.node,'epoch':self.epoch,'cursor':end,'objects':objects,'more':more}

    def cursor(self, peer: str) -> dict:
        with self.lock:
            row=self.db.execute('SELECT epoch,seq,last_ok FROM cursors WHERE peer=?',(peer,)).fetchone()
            return dict(row) if row else {'epoch':None,'seq':0,'last_ok':None}

    def receive(self, actor: Principal, page: dict, *, peer_id: str) -> dict:
        if not ({'relay','writer'} & actor.roles):raise MemoryError('BLOCKED_PERMISSION')
        if not isinstance(page,dict) or page.get('version')!=VERSION: raise MemoryError('UNSUPPORTED_SCHEMA')
        if page.get('node')!=peer_id: raise MemoryError('PEER_ID_MISMATCH')
        epoch=identifier(page.get('epoch'));seq=page.get('cursor');objects=page.get('objects')
        if not isinstance(seq,int) or isinstance(seq,bool) or seq<0 or not isinstance(objects,list) or len(objects)>MAX_PAGE or len(canonical(page).encode())>MAX_PAGE_BYTES+8192:
            raise MemoryError('INVALID_PAGE')
        accepted=0
        with self.lock,self.db:
            old=self.cursor(peer_id)
            if old['epoch'] and old['epoch']!=epoch and seq!=0:
                raise MemoryError('CURSOR_RESET_REQUIRED')
            for raw in objects:
                if not isinstance(raw,dict) or set(raw)!={'id','owner','kind','project','revision','visibility','payload','hash','lease_until'}:
                    raise MemoryError('INVALID_RECORD')
                obj={k:v for k,v in raw.items() if k!='lease_until'}
                owner=identifier(obj['owner']);kind=identifier(obj['kind']);identifier(obj['project']);identifier(obj['id'])
                if not obj['id'].startswith(f'{owner}/{kind}/') or '/' in owner:raise MemoryError('OWNER_CONFLICT')
                if obj['project'] not in actor.projects:raise MemoryError('BLOCKED_PERMISSION')
                if owner==self.node or ('relay' not in actor.roles and owner!=actor.node):raise MemoryError('OWNER_CONFLICT')
                if obj['visibility'] not in ('private','shared') or (obj['visibility']=='private' and obj['payload']):raise MemoryError('PRIVATE_PAYLOAD_REFUSED')
                if not isinstance(obj['revision'],int) or isinstance(obj['revision'],bool) or obj['revision']<1:raise MemoryError('INVALID_REVISION')
                if sanitize(kind,obj['payload'])!=obj['payload'] or digest(obj)!=obj['hash']:raise MemoryError('UNSANITIZED_OR_CORRUPT')
                lease=raw['lease_until']
                if not isinstance(lease,(int,float)) or not math.isfinite(lease):raise MemoryError('INVALID_LEASE')
                accepted+=self._write(obj,lease=min(self.clock()+OFFLINE_TTL,lease))
            self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)',(peer_id,epoch,max(seq,old['seq']) if old['epoch']==epoch else seq,self.clock()))
        return {'accepted':accepted,'cursor':self.cursor(peer_id)}

    def reset_cursor(self, peer: str, epoch: str) -> None:
        with self.lock,self.db:
            previous=self.cursor(peer)
            if previous['epoch'] and previous['epoch']!=epoch:
                self.db.execute('UPDATE documents SET lease_until=0 WHERE owner=?',(peer,))
            self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,0,?)',(identifier(peer),identifier(epoch),self.clock()))

    def search(self, actor: Principal, query: str, *, limit=20) -> list[dict]:
        actor.require('reader')
        if not isinstance(query,str) or len(query)>1024 or not isinstance(limit,int) or not 1<=limit<=5000:raise MemoryError('INVALID_QUERY')
        if not query.strip(): return []
        now=self.clock();projects=sorted(actor.projects)
        sql='SELECT d.record,d.received_at,d.lease_until FROM documents d WHERE d.project IN ('+','.join('?' for _ in projects)+') AND (d.owner=? OR (d.visibility=\'shared\' AND d.received_at<=? AND d.lease_until>?))'
        args=[*projects,self.node,now,now]
        # Private content is only readable by its local owner, never by a remote node.
        if actor.node!=self.node:
            sql+=' AND d.visibility=\'shared\''
        if self.fts:
            sql+=' AND d.id IN (SELECT id FROM memory_fts WHERE memory_fts MATCH ?)'
            args.append(' AND '.join('"'+word.replace('"','""')+'"' for word in query.split()))
        else:
            for word in query.split():sql+=' AND instr(lower(d.search_text),lower(?))>0';args.append(word)
        sql+=' ORDER BY d.id LIMIT ?';args.append(limit)
        with self.lock:
            rows=self.db.execute(sql,args).fetchall()
        return [{**json.loads(row['record']),'cached':json.loads(row['record'])['owner']!=self.node,'lease_until':row['lease_until'],'observed_at':row['received_at'],'untrusted_output':True} for row in rows]

    def close(self):
        self.db.close()
