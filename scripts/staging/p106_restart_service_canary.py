#!/usr/bin/env python3
# Staging only. Supply TMCP_P106_STAGING_ROOT and TMCP_P106_CLAUDE_BIN.
# Run under an isolated systemd --user transient unit with Restart=on-failure.
# This script intentionally exits 42 once after task A QA to prove restart recovery.
import json, os, subprocess, sys, time, traceback
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[2]
ROOT=Path(os.environ["TMCP_P106_STAGING_ROOT"]).expanduser().resolve()
if not ("/p106-" in str(ROOT) or str(ROOT).startswith("/tmp/p106-")):
    raise RuntimeError("P106_CANARY_REQUIRES_ISOLATED_ROOT")
sys.path.insert(0,str(SOURCE))
from tests.test_autopilot import World, PROJECT
from terminal_mcp import autopilot as ap
from terminal_mcp.queue_cli_worker import QueueStoreCLIWorker

ROOT.mkdir(parents=True,exist_ok=True)
WORLD=ROOT/"world"
WORLD.mkdir(exist_ok=True)
REPO=ROOT/"repo"
IDS=ROOT/"ids.json"
EVENTS=ROOT/"stage-events.jsonl"
FAIL=ROOT/"failed.json"
DONE=ROOT/"completed.json"
RESTART=ROOT/"restart.marker"

def record(kind, **kw):
    item={"at":time.time(),"kind":kind,"pid":os.getpid(),**kw}
    with EVENTS.open("a") as f: f.write(json.dumps(item,sort_keys=True)+"\n")
    print(item,flush=True)

def git(*args):
    return subprocess.run(["git",*args],cwd=REPO,check=True,capture_output=True,text=True).stdout.strip()

def run():
    if DONE.exists(): record("ALREADY_COMPLETED_NO_REPLAY"); return
    if FAIL.exists(): record("PREVIOUS_FAILURE_NO_REPLAY"); return
    world=World(WORLD,lanes=("synthetic-lane",))
    if not IDS.exists():
        REPO.mkdir(exist_ok=True)
        git("init","-q")
        git("switch","-c","staging/restart-canary")
        git("config","user.name","P106 Canary")
        git("config","user.email","canary@example.invalid")
        (REPO/"README.md").write_text("isolated P106 staging only\n")
        git("add","README.md")
        git("commit","-qm","staging baseline")
        a=world.add("synthetic-lane","A",prompt="Create A.txt with content exactly acceptance A and newline. Only edit A.txt with the Write tool; do not run Bash or git.")
        b=world.add("synthetic-lane","B",depends_on=[a],prompt="Create B.txt with content exactly acceptance B and newline. Only edit B.txt with the Write tool; do not run Bash or git.")
        IDS.write_text(json.dumps({"A":a,"B":b}))
        record("TASKS_PERSISTED",A=a,B=b)
    ids=json.loads(IDS.read_text())
    cli=QueueStoreCLIWorker(world.store,project_id=PROJECT,lane="synthetic-lane",config={
        "enabled":True,"cli":"claude","executable":os.environ["TMCP_P106_CLAUDE_BIN"],
        "working_directory":str(REPO),"isolated_worktree":True,
        "commit_mode":"worker","allowed_paths":["A.txt","B.txt"],
        "test_command":[sys.executable,"-c","from pathlib import Path;assert Path('A.txt').is_file() or Path('B.txt').is_file()"],
        "timeout_seconds":120,"heartbeat_seconds":2
    },worker_id="p106-pilot-"+str(os.getpid()))
    pilot=world.autopilot(instance="p106-controller-"+str(os.getpid()),
        dispatcher=ap.ProjectDispatcher(world.dispatcher,{"synthetic-lane":cli}))
    record("CONTROLLER_START",restart_seen=RESTART.exists())
    start=time.monotonic()
    while time.monotonic()-start<170:
        pilot.tick(PROJECT)
        for key in ("A","B"):
            task_id=ids[key]
            if world.status(task_id)=="VERIFYING":
                receipt=(world.store.get_task(task_id).metadata or {}).get("cli_worker") or {}
                job=world.verify.claim_next(verifier="independent-synthetic-qa",project_id=PROJECT)
                if not job or job.task_id!=task_id:
                    continue
                evidence={"tests_passed":1,"tests_failed":0,"exit_code":0,
                  "command":"staging independent verify of exact worker SHA",
                  "commit_sha":job.commit_sha}
                answer=world.verify.complete(job.id,job.claim_token,evidence=evidence)
                if not answer.get("ok"): raise RuntimeError("QA_REJECTED_"+str(answer))
                record("QA_ACCEPTED",step=key,task_id=task_id,sha=receipt.get("commit_sha"),worker_pid=receipt.get("pid"))
                if key=="A" and not RESTART.exists():
                    RESTART.write_text(str(os.getpid()))
                    record("INTENTIONAL_CONTROLLER_CRASH_AFTER_A_QA",exit=42)
                    os._exit(42)
        if world.status(ids["B"]) in ("COMPLETED","DONE_VERIFIED"):
            statuses={k:world.status(v) for k,v in ids.items()}
            commits=git("log","-3","--format=%H %s")
            if not (REPO/"A.txt").is_file() or not (REPO/"B.txt").is_file():
                raise RuntimeError("MISSING_OUTPUTS")
            if git("status","--porcelain"):
                raise RuntimeError("DIRTY_OUTPUT")
            DONE.write_text(json.dumps({"states":statuses,"commits":commits,"restarted":RESTART.exists(),"pid":os.getpid()},indent=2))
            record("STAGING_PASS",**statuses)
            return
        if int(time.monotonic()-start)%15==0:
            record("PROGRESS",A=world.status(ids["A"]),B=world.status(ids["B"]))
        time.sleep(1)
    raise TimeoutError("P106_STAGING_DEADLINE_EXCEEDED")

try:
    run()
except Exception as e:
    record("STAGING_BLOCKED",reason=type(e).__name__+":"+str(e)[:300])
    FAIL.write_text(json.dumps({"reason":str(e),"trace":traceback.format_exc()},indent=2))
