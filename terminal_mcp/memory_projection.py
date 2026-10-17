"""Explicit bounded projections; no source/transcript copying or skill execution."""
from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import quote

import yaml

from .unified_memory import MemoryError, MemoryStore, Principal

MAX_FILE = 65536


def key(text):
    return hashlib.sha256(str(text).encode()).hexdigest()[:24]


def approved(path, roots):
    resolved=Path(path).resolve()
    if not any(resolved.is_relative_to(root) for root in roots):raise MemoryError('OUTSIDE_APPROVED_ROOT')
    name=resolved.name.lower()
    if name.startswith('.env') or name in ('id_rsa','id_ed25519','credentials','credentials.json') or resolved.suffix.lower() in ('.pem','.key'):
        raise MemoryError('CREDENTIAL_FILE_REFUSED')
    return resolved


def read_file(path, roots, limit=MAX_FILE):
    path=approved(path,roots)
    with path.open('rb') as f:data=f.read(limit+1)
    if len(data)>limit:raise MemoryError('SOURCE_TOO_LARGE')
    return data.decode('utf-8')


def git(root,*args):
    result=subprocess.run(['git','--no-optional-locks','-C',str(root),*args],capture_output=True,text=True,timeout=5)
    if result.returncode:raise MemoryError('GIT_UNAVAILABLE')
    if len(result.stdout.encode())>MAX_FILE:raise MemoryError('SOURCE_TOO_LARGE')
    return result.stdout.strip()


def project_sources(store: MemoryStore, projects: dict, *, shared_projects=(), session_db=None, session_authorized=None) -> dict:
    actor=Principal(store.node,frozenset({'reader','writer'}),frozenset(projects))
    result={'published':0,'errors':[]};seen=set()
    for project,config in projects.items():
        roots=[Path(r).resolve() for r in config.get('roots',[])]
        visibility='shared' if project in shared_projects else 'private'
        def put(kind,identity,payload):
            obj=store.publish(actor,kind,key(project+':'+str(identity)),project,payload,visibility=visibility,projected=True)
            seen.add(obj['id'])
            result['published']+=1
            return obj
        def attempt(fn):
            try:fn()
            except (MemoryError,OSError,ValueError,TypeError,KeyError,AttributeError,sqlite3.Error,subprocess.SubprocessError,yaml.YAMLError):
                # No untrusted filename, command stderr, source text, or credential in logs.
                result['errors'].append({'project':project,'error':'SOURCE_REFUSED_OR_UNAVAILABLE'})
        put('project',project,{'title':project,'status':'configured'})
        put('node',project,{'title':store.node,'platform':platform.system(),'status':'observed'})
        for root in roots:
            def repository():
                repo=git(root,'rev-parse','--show-toplevel');approved(repo,roots)
                try:remote=git(root,'remote','get-url','origin')
                except MemoryError:remote=''
                try:head=git(root,'rev-parse','HEAD')
                except MemoryError:head=''
                branch=git(root,'symbolic-ref','--short','-q','HEAD') if head=='' else git(root,'rev-parse','--abbrev-ref','HEAD')
                record=put('repo',repo,{'title':root.name,'path':str(root),'remote':remote,'head':head,'branch':branch,'dirty':bool(git(root,'status','--porcelain','--untracked-files=no'))})
                repo_id=record['id']
                for line in git(root,'for-each-ref','--format=%(refname:short)\t%(objectname)','refs/heads').splitlines():
                    name,sha=line.split('\t',1)
                    put('branch',repo+'#'+name,{'title':name,'head':sha,'repo_id':repo_id})
                # Branch/worktree metadata only. Never open paths outside approved roots.
                for block in git(root,'worktree','list','--porcelain').split('\n\n'):
                    facts=dict(line.split(' ',1) for line in block.splitlines() if ' ' in line)
                    path=facts.get('worktree')
                    if path and any(Path(path).resolve().is_relative_to(r) for r in roots):
                        put('worktree',path,{'title':Path(path).name,'path':path,'head':facts.get('HEAD',''),'branch':facts.get('branch',''),'repo_id':repo_id})
            attempt(repository)
            backlog=root/'.terminal-mcp/backlog.json'
            if backlog.exists():
                def tasks():
                    data=json.loads(read_file(backlog,roots,1048576))
                    items=data.get('items',[])
                    if not isinstance(items,list) or len(items)>2000:raise MemoryError('SOURCE_TOO_LARGE')
                    for item in items:
                        put('task',str(root)+'#'+str(item['id']),{k:item[k] for k in ('title','status','priority','session','branch') if k in item})
                attempt(tasks)
        for skill in config.get('skills',[]):
            def manifest():
                path=Path(skill)/'SKILL.md';raw=read_file(path,roots)
                front=raw.split('---',2)
                meta=yaml.safe_load(front[1]) if len(front)==3 and not front[0].strip() else {}
                if not isinstance(meta,dict):raise MemoryError('INVALID_MANIFEST')
                put('skill',path,{'title':str(meta.get('name',Path(skill).name)),'version':str(meta.get('version','unknown')),'digest':hashlib.sha256(raw.encode()).hexdigest(),'path':str(path)})
            attempt(manifest)
        for note in config.get('notes',[]):
            def checkpoint():
                kind=note.get('kind','note')
                if kind not in ('note','checkpoint'):raise MemoryError('INVALID_KIND')
                path=note['path'];body=read_file(path,roots)
                put(kind,path,{'title':Path(path).name,'body':body,'source':str(Path(path).resolve())})
            attempt(checkpoint)
        for index,service in enumerate(config.get('services',[])):
            attempt(lambda:put('service',project+':'+str(index),{k:service[k] for k in ('title','status','endpoint') if k in service}))
        if session_db:
            def sessions():
                # mode=ro prevents even accidental schema creation/migration in source DBs.
                with sqlite3.connect('file:'+quote(str(Path(session_db).resolve()))+'?mode=ro',uri=True) as db:
                    db.row_factory=sqlite3.Row
                    rows=db.execute('SELECT session_name,status,agent_type,git_branch,last_commit,repo_root FROM session_records WHERE node_id=? AND read_granted=1 AND repo_root IN ('+','.join('?' for _ in roots)+') LIMIT 2001',[store.node,*map(str,roots)]).fetchall()
                    if len(rows)>2000:raise MemoryError('SOURCE_TOO_LARGE')
                    for row in rows:
                        if session_authorized is None or not session_authorized(row['session_name']):continue
                        put('session',row['session_name'],{'title':row['session_name'],'status':row['status'],'agent_type':row['agent_type'],'branch':row['git_branch'],'head':row['last_commit']})
            attempt(sessions)
    store.reconcile_projection(seen)
    return result
