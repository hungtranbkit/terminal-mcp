#!/usr/bin/env python3
"""Bounded, conservative Terminal MCP tmux session cleanup."""
import argparse, subprocess, pathlib, sqlite3, time, json, fcntl, os, re
def cmd(*xs):
    try:
        p=subprocess.run(xs,text=True,capture_output=True,timeout=5)
        return p.returncode,p.stdout.strip()
    except (OSError,subprocess.TimeoutExpired):
        return 99,""
def details(s):
    rc,out=cmd("tmux","list-panes","-t",s,"-F","#{pane_current_command}|#{pane_pid}|#{session_attached}|#{session_activity}|#{window_panes}")
    if rc or len(out.splitlines())!=1:return None
    a=out.split("|")
    if len(a)!=5 or not a[1].isdigit() or not a[3].isdigit():return None
    return a
def unfinished(s):
    db=pathlib.Path.home()/".local/state/terminal-mcp/direct_tasks.db"
    if not db.exists():return False
    try:
        with sqlite3.connect("file:"+str(db)+"?mode=ro",uri=True,timeout=1) as c:
            return c.execute("SELECT count(*) FROM direct_tasks WHERE target=? AND state NOT IN ('DONE','COMPLETED','FAILED','CANCELLED','BLOCKED')",(s,)).fetchone()[0]>0
    except sqlite3.Error:return True
def safe(s, pressure):
    if not re.match(r"^(hp-|dell-|nec1-|m910-)",s) or any(t in s for t in ("-doctor-","-controller-","-maintenance-")):return False,"protected"
    a=details(s)
    if not a:return False,"pane-unavailable"
    process,pid,attached,activity,panes=a
    if attached!="0" or panes!="1" or unfinished(s):return False,"attached/multi-pane/queued"
    age=time.time()-int(activity)
    if process in ("bash","sh","zsh","fish","dash"):
        if age<86400:return False,"recent-shell"
    elif process=="codex":
        if age < (86400 if pressure else 172800):return False,"recent-codex"
        rc,screen=cmd("tmux","capture-pane","-t",s,"-p","-S","-16")
        if rc or "Ask Codex to do anything" not in screen or any(x in screen for x in ("Trust this folder?","Press enter to confirm","Would you like to run the following command?")):return False,"no-empty-composer"
    else:return False,"agent-active-or-unknown"
    rc,ch=cmd("ps","--ppid",pid,"-o","comm=")
    if rc:return False,"child-check-failed"
    kids=set(ch.splitlines()) if ch else set()
    allowed=set() if process!="codex" else {"codex-code-mode","MainThread","node_repl","npm exec chrome"}
    if kids-allowed:return False,"active-children"
    return True,"idle-verified"
def cleanup_temp(apply):
    count=bytes_freed=0
    roots=(".cache/terminal-mcp/tmp",".local/state/terminal-mcp/tmp")
    for relative in roots:
        root=pathlib.Path.home()/relative
        if not root.is_dir() or root.is_symlink():continue
        for p in root.rglob("*"):
            if p.is_symlink() or not p.is_file() or not p.name.endswith((".tmp",".part",".partial")):continue
            try:
                st=p.stat()
                if time.time()-st.st_mtime<7*86400 or bytes_freed+st.st_size>134217728:continue
                if apply:p.unlink()
                count+=1;bytes_freed+=st.st_size
            except OSError:pass
    return {"files":count,"bytes":bytes_freed}
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--node",required=True);ap.add_argument("--cap",type=int,default=20)
    ap.add_argument("--apply",action="store_true");ap.add_argument("--limit",type=int,default=6)
    a=ap.parse_args()
    state=pathlib.Path.home()/".local/state/terminal-mcp/node-janitor"
    state.mkdir(parents=True,exist_ok=True)
    with (state/"lock").open("w") as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        rc,out=cmd("tmux","list-sessions","-F","#{session_name}")
        names=out.splitlines() if not rc else []
        pressure=len(names)>=max(1,a.cap-2)
        report={"node":a.node,"time":time.time(),"before":len(names),"pressure":pressure,"applied":a.apply,"reclaimed":[],"skipped":{}}
        for s in names:
            if len(report["reclaimed"])>=a.limit:break
            ok,reason=safe(s,pressure)
            if not ok:
                report["skipped"][s]=reason
                continue
            first=details(s)
            time.sleep(.15)
            ok,reason2=safe(s,pressure)
            if not ok or details(s)!=first:
                report["skipped"][s]="changed/"+reason2
                continue
            if a.apply and cmd("tmux","kill-session","-t","="+s)[0]:
                report["skipped"][s]="kill-failed"
                continue
            report["reclaimed"].append(s)
        report["temporary"]=cleanup_temp(a.apply)
        rc,out=cmd("tmux","list-sessions","-F","#{session_name}")
        report["after"]=len(out.splitlines()) if not rc else 0
        tmp=state/"last.json.tmp"
        tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        tmp.replace(state/"last.json")
        with (state/"audit.jsonl").open("a") as f:f.write(json.dumps({k:v for k,v in report.items() if k!="skipped"},ensure_ascii=False)+"\n")
        print(json.dumps({k:v for k,v in report.items() if k!="skipped"},ensure_ascii=False))
if __name__=="__main__":main()
