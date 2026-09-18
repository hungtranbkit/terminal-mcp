import importlib.util, json, sys
from pathlib import Path
P=Path(__file__).parents[1]/'deploy'/'fleet-rollout.py'
s=importlib.util.spec_from_file_location('fleet_rollout',P); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m)

def test_linux_is_pinned_not_pull():
 n=m.Node('n','linux','h','/repo'); pre,d,r=m.commands(n,'a'*40)
 assert 'git fetch origin' in d and 'git checkout --detach' in d and 'git pull' not in d
 assert 'systemctl --user restart' in d and '{previous}' in r

def test_preflight_refuses_tracked_dirty_but_ignores_node_secrets():
 n=m.Node('n','linux','h','/repo'); pre,_,_=m.commands(n,'a'*40)
 assert '--untracked-files=no' in pre

def test_windows_uses_same_sha_and_only_agent_task():
 n=m.Node('w','windows','h','C:\\tmcp','Terminal MCP Node Agent'); _,d,r=m.commands(n,'b'*40)
 assert 'b'*40 in d and 'git checkout --detach' in d and 'Start-ScheduledTask' in d and '{previous}' in r

def test_manifest_enabled_filter(tmp_path):
 p=tmp_path/'m.json'; p.write_text(json.dumps({'nodes':[{'node_id':'a','platform':'linux','ssh':'a','repo_dir':'/a'},{'node_id':'b','platform':'linux','ssh':'b','repo_dir':'/b','enabled':False}]}))
 assert [x.node_id for x in m.load(p)]==['a']
