import pytest
from tests.test_issue118_durable_start import rig, start, IDLE

@pytest.mark.parametrize("action",["send","send_wait"])
def test_long_alias_is_same_durable_task(rig,action):
    tools,ctl,_=rig
    first=tools.turn(action=action,target="codex",text="new isolated goal",long_task=True,request_key="same",timeout=1)
    assert first["task_id"].startswith("dt_")
    again=start(tools,text="new isolated goal",key="same")
    assert again["task_id"]==first["task_id"] and again["deduplicated"]
    assert len(ctl.send_calls)==1 and ctl.send_calls[0][4]["require_idle"] is True

@pytest.mark.parametrize("outcome",["DONE","BLOCKED"])
def test_fast_shell_marker_without_echo(rig,outcome):
    tools,ctl,clock=rig
    ctl.statuses["codex"]=[{"state":"IDLE","exists":True,"reason":"shell prompt is back at the bottom of the pane; the command has finished"}]
    task=tools.direct_tasks.start("codex",text="printf 'TMCP-%s:%s\\n' "+outcome+" {TMCP_TASK_ID}",mode="shell",idempotency_key="fast-shell")["task"]
    tid=task["task_id"]
    assert task["receipt"]=="SUBMIT_CONFIRMED"
    ctl.tails["codex"]="output\nTMCP-"+outcome+":"+tid+(" local failure" if outcome=="BLOCKED" else "")+"\nmac$ "
    clock.now+=4
    tools.direct_tasks.tick()
    view=tools.direct_tasks.get(tid,refresh=False)["task"]
    assert view["state"]==outcome and view["dispatches"]==1
    assert view["receipt"]=="EXECUTION_ACCEPTED"
    assert len(ctl.send_calls)==1

@pytest.mark.parametrize("kind",["literal","wrong-id","no-ack","echo","waiting"])
def test_fast_marker_negative(rig,kind):
    tools,ctl,clock=rig
    ctl.statuses["codex"]=[{"state":"IDLE","exists":True,"reason":"shell prompt is back at the bottom of the pane; the command has finished"}]
    text="echo TMCP-DONE:{TMCP_TASK_ID}" if kind=="literal" else "printf 'TMCP-%s:%s\\n' DONE {TMCP_TASK_ID}"
    task=tools.direct_tasks.start("codex",text=text,mode="shell",idempotency_key="negative")["task"]
    tid=task["task_id"]
    if kind=="no-ack":
        tools.direct_tasks.store.update(tid,receipt="DISPATCH_UNCERTAIN")
    if kind=="waiting":
        ctl.statuses["codex"]=[{"state":"WAITING_INPUT","exists":True,"input_required":True}]
    ctl.tails["codex"]=("$ echo " if kind=="echo" else "")+"TMCP-DONE:"+("dt_ffffffffffffffff" if kind=="wrong-id" else tid)+"\nmac$ "
    clock.now+=4
    tools.direct_tasks.tick()
    view=tools.direct_tasks.get(tid,refresh=False)["task"]
    assert view["state"]!="DONE"
    assert len(ctl.send_calls)==1
