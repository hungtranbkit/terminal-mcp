#!/usr/bin/env python3
"""Bounded P106 Dell pilot: single synthetic QueueStore project; no production work."""
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[2]
BASE=Path("/home/dell/workspace/.terminal-mcp-worktrees")
ROOT=Path(os.environ["TMCP_P106_PILOT_ROOT"]).expanduser().resolve()
if not ROOT.is_relative_to(BASE) or not ROOT.name.startswith("p106-"):
    raise RuntimeError("PILOT_ROOT_SCOPE_INVALID")
CLI=os.environ["TMCP_P106_CLAUDE_BIN"]
sys.path.insert(0,str(SOURCE))
from tests.test_autopilot import PROJECT,World
from terminal_mcp import autopilot as ap
from terminal_mcp.queue_cli_worker import QueueStoreCLIWorker

ROOT.mkdir(parents=True,exist_ok=True)
WORLD=ROOT/"world"
REPO=ROOT/"repo"
IDS=ROOT/"ids.json"
HEALTH=ROOT/"health.json"
EVENTS=ROOT/"events.jsonl"
DONE=ROOT/"completed.json"
PAUSE=ROOT/"PAUSE"
FAILED=ROOT/"failed.json"
STOP=False
def on_stop(sig,frame):
    global STOP
    STOP=True
signal.signal(signal.SIGTERM,on_stop)
signal.signal(signal.SIGINT,on_stop)

def event(kind,**x):
    obj={"at":time.time(),"kind":kind,"pid":os.getpid(),**x}
    with EVENTS.open("a") as f: f.write(json.dumps(obj,sort_keys=True)+"\n")
    print(json.dumps(obj,sort_keys=True),flush=True)

def status(state,**x):
    obj={"updated_at":time.time(),"pid":os.getpid(),"state":state,
         "project":PROJECT,"scope":"synthetic_dell_pilot",
         "production_enabled":False,
         "code_sha":os.environ["TMCP_P106_CODE_SHA"],**x}
    tmp=HEALTH.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj,indent=2))
    tmp.replace(HEALTH)

def git(*args):
    return subprocess.run(["git",*args],cwd=REPO,check=True,
                          capture_output=True,text=True).stdout.strip()

def init(world):
    if IDS.exists(): return json.loads(IDS.read_text())
    if REPO.exists(): raise RuntimeError("PILOT_REPO_WITHOUT_LEDGER")
    REPO.mkdir()
    git("init","-q")
    git("switch","-c","staging/p106-dell")
    git("config","user.name","P106 Canary")
    git("config","user.email","p106@example.invalid")
    (REPO/"README.md").write_text("Synthetic P106 Dell staging, no production data.\n")
    git("add","--","README.md")
    git("commit","-qm","staging baseline")
    a=world.add("synthetic-lane","A",prompt="Create ONLY A.txt containing acceptance A and a newline. Use Write/Edit; do not run Bash or git.")
    b=world.add("synthetic-lane","B",depends_on=[a],prompt="Create ONLY B.txt containing acceptance B and a newline. Use Write/Edit; do not run Bash or git.")
    ids={"A":a,"B":b}
    IDS.write_text(json.dumps(ids,indent=2))
    event("SEEDED",**ids)
    return ids

def verified(k):
    p=REPO/(k+".txt")
    return p.is_file() and p.read_text().strip()=="acceptance "+k

def run():
    WORLD.mkdir(exist_ok=True)
    w=World(WORLD,lanes=("synthetic-lane",))
    ids=init(w)
    worker=QueueStoreCLIWorker(w.store,project_id=PROJECT,lane="synthetic-lane",
        config={"enabled":True,"cli":"claude","executable":CLI,
                "working_directory":str(REPO),"isolated_worktree":True,
                "commit_mode":"worker","allowed_paths":["A.txt","B.txt"],
                "test_command":[sys.executable,"-c",
                    "from pathlib import Path;assert Path('A.txt').exists() or Path('B.txt').exists()"],
                "timeout_seconds":120,"heartbeat_seconds":2},
        worker_id="p106-dell-"+str(os.getpid()))
    ctl=w.autopilot(instance="p106-dell-"+str(os.getpid()),
        dispatcher=ap.ProjectDispatcher(w.dispatcher,{"synthetic-lane":worker}))
    event("SERVICE_STARTED")
    previous=None
    while not STOP:
        if PAUSE.exists(): state="PAUSED"
        elif FAILED.exists(): state="BLOCKED"
        elif DONE.exists(): state="READY_IDLE"
        else:
            ctl.tick(PROJECT)
            for k in ("A","B"):
                tid=ids[k]
                if w.status(tid)!="VERIFYING": continue
                if not verified(k): raise RuntimeError("QA_CONTENT_"+k)
                job=w.verify.claim_next(verifier="independent-synthetic-qa",project_id=PROJECT)
                if not job or job.task_id!=tid: continue
                result=w.verify.complete(job.id,job.claim_token,evidence={
                    "tests_passed":1,"tests_failed":0,"exit_code":0,
                    "command":"synthetic exact content verification",
                    "commit_sha":job.commit_sha})
                if not result.get("ok"): raise RuntimeError("QA_FAILED_"+k)
                receipt=(w.store.get_task(tid).metadata or {}).get("cli_worker") or {}
                event("QA_ACCEPTED",task=k,commit=receipt.get("commit_sha"),worker_pid=receipt.get("pid"))
            current={k:w.status(v) for k,v in ids.items()}
            if all(v=="COMPLETED" for v in current.values()):
                if not (verified("A") and verified("B")): raise RuntimeError("MISSING_A_B")
                if git("status","--porcelain"): raise RuntimeError("DIRTY_PILOT_REPO")
                proof={"states":current,"commits":git("log","-3","--format=%H %s"),
                       "completed_at":time.time()}
                DONE.write_text(json.dumps(proof,indent=2))
                event("PILOT_COMPLETE",**current)
                state="READY_IDLE"
            else: state="RUNNING"
        current={k:w.status(v) for k,v in ids.items()}
        status(state,tasks=current,completed=DONE.exists(),paused=PAUSE.exists())
        now=(state,current["A"],current["B"])
        if now!=previous:
            event("STATE_CHANGED",state=state,**current)
            previous=now
        time.sleep(3)
    status("STOPPED",tasks={k:w.status(v) for k,v in ids.items()})
    event("SERVICE_STOPPED")

if __name__=="__main__":
    try: run()
    except Exception as exc:
        event("FATAL",reason=type(exc).__name__+":"+str(exc)[:250])
        FAILED.write_text(json.dumps({"error":str(exc),"trace":traceback.format_exc()},indent=2))
        status("BLOCKED",reason=type(exc).__name__)
        sys.exit(2)
