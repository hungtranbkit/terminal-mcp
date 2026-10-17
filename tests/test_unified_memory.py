import json
import pytest
from terminal_mcp.unified_memory import MemoryStore, Principal, MemoryError


def principal(node='a', roles=('reader','writer'), projects=('p',)):
    return Principal(node, frozenset(roles), frozenset(projects))


def store(tmp_path, node='a', clock=None):
    return MemoryStore(tmp_path / (node+'.db'), node, clock=clock)


def test_private_default_and_redacted_before_disk(tmp_path):
    s=store(tmp_path)
    obj=s.publish(principal(), 'note', 'one', 'p', {'title':'note','body':'Authorization: Bearer '+ 'z'*40})
    assert obj['visibility']=='private'
    assert 'z'*40 not in json.dumps(obj)
    assert not s.page(principal('controller', ('reader','relay')), 0)['objects']
    assert len(s.search(principal(), 'note'))==1
    assert 'z'*40 not in s.db.execute('SELECT record FROM journal').fetchone()[0]


def test_rbac_filters_before_limit_and_forbids_scope_escalation(tmp_path):
    s=store(tmp_path)
    for i in range(10):
        s.publish(principal(projects=('secret',)), 'note', str(i), 'secret', {'title':'needle'})
    s.publish(principal(), 'note','ok','p',{'title':'needle'})
    assert len(s.search(principal(), 'needle', limit=1))==1
    with pytest.raises(MemoryError): s.publish(principal(roles=('reader',)), 'note','x','p',{})
    with pytest.raises(MemoryError): s.publish(principal(), 'note','x','other',{})


def test_paginated_sync_restart_replay_and_offline_expiry(tmp_path):
    now=[1000.0]
    a=store(tmp_path,'a',lambda:now[0]); b=store(tmp_path,'b',lambda:now[0])
    for i in range(4501):
        a.publish(principal(), 'task', str(i),'p',{'title':f'needle {i}','status':'READY'}, visibility='shared')
    peer=principal('controller',('reader','relay'))
    cursor=0
    while True:
        page=a.page(peer,cursor,limit=200)
        b.receive(peer,page,peer_id='a')
        b.receive(peer,page,peer_id='a')
        cursor=page['cursor']
        if not page['more']:break
    assert b.db.execute('SELECT count(*) FROM journal').fetchone()[0]==4501
    a.close(); b.close()
    b=store(tmp_path,'b',lambda:now[0]); a=store(tmp_path,'a',lambda:now[0])
    assert b.cursor('a')['seq']==4501
    assert len(b.search(principal('b'), 'needle',limit=5000))==4501
    assert a.page(peer,cursor)['objects']==[]
    now[0]+=901
    assert b.search(principal('b'),'needle')==[]
    assert b.db.execute('SELECT count(*) FROM documents').fetchone()[0]==4501


def test_forgery_conflict_and_atomic_reject(tmp_path):
    a=store(tmp_path,'a');b=store(tmp_path,'b')
    a.publish(principal(),'note','x','p',{'title':'original'},visibility='shared')
    page=a.page(principal('controller',('reader','relay')),0)
    attacker=principal('evil',('writer',))
    with pytest.raises(MemoryError):b.receive(attacker,page,peer_id='a')
    assert b.cursor('a')['seq']==0
    b.receive(principal('controller',('relay',)),page,peer_id='a')
    bad=json.loads(json.dumps(page));bad['objects'][0]['owner']='b'
    with pytest.raises(MemoryError):b.receive(principal('controller',('relay',)),bad,peer_id='a')
    bad=json.loads(json.dumps(page));bad['objects'][0]['payload']['title']='different'
    with pytest.raises(MemoryError):b.receive(principal('controller',('relay',)),bad,peer_id='a')
    assert b.db.execute('SELECT count(*) FROM journal').fetchone()[0]==1


def test_retains_revisions_and_hides_withdrawn_shared_content(tmp_path):
    a=store(tmp_path);b=store(tmp_path,'b');relay=principal('controller',('relay','reader'))
    one=a.publish(principal(),'note','x','p',{'title':'needle'},visibility='shared')
    b.receive(relay,a.page(relay,0),peer_id='a')
    assert b.search(principal('b'),'needle')
    a.publish(principal(),'note','x','p',{'title':'private new body'},visibility='private')
    page=a.page(relay,1)
    assert 'private new body' not in json.dumps(page)
    b.receive(relay,page,peer_id='a')
    assert b.search(principal('b'),'needle')==[]
    assert a.db.execute('SELECT count(*) FROM journal').fetchone()[0]==2


def test_epoch_and_clock_rollback_fail_closed(tmp_path):
    now=[1000.0];a=store(tmp_path,'a',lambda:now[0]);b=store(tmp_path,'b',lambda:now[0])
    relay=principal('controller',('relay','reader'))
    a.publish(principal(),'note','x','p',{'title':'needle'},visibility='shared')
    b.receive(relay,a.page(relay,0),peer_id='a')
    now[0]=900
    assert b.search(principal('b'),'needle')==[]
    with pytest.raises(MemoryError):a.page(relay,1,epoch='wrong')


def test_unknown_payload_fields_and_secret_files_refused(tmp_path):
    a=store(tmp_path)
    with pytest.raises(MemoryError):a.publish(principal(),'skill','x','p',{'body':'do something'})
    with pytest.raises(MemoryError):a.publish(principal(),'note','x','p',{'body':'-----BEGIN PRIVATE KEY-----\nsecret'})


def test_bootstrap_after_privacy_change_never_replays_old_body(tmp_path):
    a=store(tmp_path);b=store(tmp_path,'b');relay=principal('controller',('relay','reader'))
    a.publish(principal(),'note','x','p',{'title':'old needle'},visibility='shared')
    b.receive(relay,a.page(relay,0),peer_id='a')
    a.publish(principal(),'note','x','p',{'body':'new private'},visibility='private')
    page=a.page(relay,0)
    assert 'old needle' not in json.dumps(page)
    b.receive(relay,page,peer_id='a')
    assert not b.search(principal('b'),'needle')


def test_replay_only_renews_to_original_absolute_lease(tmp_path):
    now=[1000.0];a=store(tmp_path,'a',lambda:now[0]);b=store(tmp_path,'b',lambda:now[0])
    relay=principal('controller',('reader','relay'))
    a.publish(principal(),'note','x','p',{'title':'needle'},visibility='shared')
    first=a.page(relay,0);b.receive(relay,first,peer_id='a')
    now[0]=1800
    b.receive(relay,a.page(relay,0),peer_id='a')
    now[0]=2000
    assert b.search(principal('b'),'needle')
    b.receive(relay,first,peer_id='a')
    now[0]=2801
    assert not b.search(principal('b'),'needle')


def test_replayed_page_cannot_reset_clock_rollback_watermark(tmp_path):
    now=[10000.0];a=store(tmp_path,'a',lambda:now[0]);b=store(tmp_path,'b',lambda:now[0])
    relay=principal('controller',('reader','relay'))
    a.publish(principal(),'note','x','p',{'title':'needle'},visibility='shared')
    page=a.page(relay,0);b.receive(relay,page,peer_id='a')
    now[0]=1000
    assert not b.search(principal('b'),'needle')
    b.receive(relay,page,peer_id='a')
    now[0]=1901
    assert not b.search(principal('b'),'needle')
    assert not b.page(relay,0)['objects']
