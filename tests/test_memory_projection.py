import json
import subprocess
import pytest
from terminal_mcp.unified_memory import MemoryStore,Principal,MemoryError
from terminal_mcp.memory_projection import project_sources


def test_git_skills_tasks_notes_services_without_source_or_skill_body(tmp_path):
    root=tmp_path/'repo';root.mkdir()
    subprocess.run(['git','init',str(root)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(root),'remote','add','origin','https://user:fake-password@example.com/org/repo.git?token=fake'],check=True)
    (root/'secret_source.py').write_text('DO_NOT_COPY_SOURCE')
    skill=root/'skill';skill.mkdir();(skill/'SKILL.md').write_text('---\nname: safe-skill\nversion: 1\n---\nDO_NOT_COPY_SKILL_BODY')
    (root/'note.md').write_text('Checkpoint needle\nAuthorization: Bearer '+'z'*40)
    (root/'.terminal-mcp').mkdir();(root/'.terminal-mcp/backlog.json').write_text(json.dumps({'items':[{'id':'t','title':'Task','status':'READY','description':'DO_NOT_COPY_DESCRIPTION'}]}))
    s=MemoryStore(tmp_path/'memory.db','a')
    result=project_sources(s,{'p':{'roots':[str(root)],'skills':[str(skill)],'notes':[{'path':str(root/'note.md'),'kind':'checkpoint'}],'services':[{'title':'api','status':'configured'}]}})
    assert not result['errors']
    records=[r[0] for r in s.db.execute('SELECT record FROM documents')]
    data=' '.join(records)
    for secret in ('DO_NOT_COPY_SOURCE','DO_NOT_COPY_SKILL_BODY','DO_NOT_COPY_DESCRIPTION','fake-password','z'*40,'?token='):
        assert secret not in data
    parsed=[json.loads(r) for r in records]
    repo_ids={r['id'] for r in parsed if r['kind']=='repo'}
    assert all(r['payload']['repo_id'] in repo_ids for r in parsed if r['kind'] in ('branch','worktree'))
    kinds={json.loads(r)['kind'] for r in records}
    assert {'repo','worktree','project','skill','task','checkpoint','service','node'}<=kinds
    assert all(json.loads(r)['visibility']=='private' for r in records)
    before=s.db.execute('SELECT count(*) FROM journal').fetchone()[0]
    project_sources(s,{'p':{'roots':[str(root)],'skills':[str(skill)],'notes':[{'path':str(root/'note.md'),'kind':'checkpoint'}],'services':[{'title':'api','status':'configured'}]}})
    assert s.db.execute('SELECT count(*) FROM journal').fetchone()[0]==before


def test_symlink_and_credential_file_refused_without_deletion(tmp_path):
    root=tmp_path/'repo';root.mkdir();secret=tmp_path/'outside.md';secret.write_text('private outside')
    (root/'link.md').symlink_to(secret);(root/'.env').write_text('PASSWORD=not-for-memory')
    s=MemoryStore(tmp_path/'memory.db','a')
    result=project_sources(s,{'p':{'roots':[str(root)],'notes':[{'path':str(root/'link.md')},{'path':str(root/'.env')}]}})
    assert len(result['errors'])>=2
    assert secret.read_text()=='private outside'
    assert not s.search(Principal('a',frozenset({'reader'}),frozenset({'p'})),'private outside')


def test_removed_sources_and_projects_withdraw_without_erasing_history(tmp_path):
    root=tmp_path/'repo';root.mkdir();note=root/'note.md';note.write_text('revocable needle')
    s=MemoryStore(tmp_path/'memory.db','a')
    config={'p':{'roots':[str(root)],'notes':[{'path':str(note)}]}}
    project_sources(s,config,shared_projects=['p'])
    actor=Principal('a',frozenset({'reader'}),frozenset({'p'}))
    assert s.search(actor,'needle')
    config['p']['notes']=[]
    project_sources(s,config,shared_projects=['p'])
    assert not s.search(actor,'needle')
    assert note.read_text()=='revocable needle'
    assert any('revocable needle' in r[0] for r in s.db.execute('SELECT record FROM journal'))
    project_sources(s,{})
    assert all(json.loads(r[0])['visibility']=='private' for r in s.db.execute('SELECT record FROM documents'))


def test_projection_catalog_failure_rolls_back_publication(tmp_path):
    import sqlite3
    s=MemoryStore(tmp_path/'memory.db','a')
    s.db.execute("CREATE TRIGGER fail_catalog BEFORE INSERT ON projected BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):project_sources(s,{'p':{}},shared_projects=['p'])
    assert s.db.execute('SELECT count(*) FROM journal').fetchone()[0]==0


def test_malformed_backlog_does_not_prevent_withdrawal(tmp_path):
    root=tmp_path/'repo';root.mkdir();(root/'.terminal-mcp').mkdir()
    note=root/'note.md';note.write_text('revocable needle')
    s=MemoryStore(tmp_path/'memory.db','a')
    config={'p':{'roots':[str(root)],'notes':[{'path':str(note)}]}}
    project_sources(s,config,shared_projects=['p'])
    config['p']['notes']=[]
    (root/'.terminal-mcp/backlog.json').write_text('["bad shape"]')
    result=project_sources(s,config,shared_projects=['p'])
    assert result['errors']
    assert not s.search(Principal('a',frozenset({'reader'}),frozenset({'p'})),'needle')
