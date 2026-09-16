#!/usr/bin/env python3
"""Pin and converge Terminal MCP node agents to one immutable Git revision.

Controller-side, sequential/canary by design. It never uses `git pull`: each node
fetches then checks out the exact requested SHA. Per-node config/tokens stay outside
Git. On failure it restores the node's previous HEAD and restarts only that agent.
"""
from __future__ import annotations
import argparse, json, shlex, subprocess, sys, urllib.request
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class Node:
    node_id: str; platform: str; ssh: str; repo_dir: str
    service: str = "terminal-node-agent.service"
    health_url: str | None = None


def run(argv, *, check=True):
    return subprocess.run(argv, text=True, capture_output=True, check=check)


def ssh(node: Node, command: str, *, check=True):
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", node.ssh, command], check=check)


def q(s: str) -> str: return shlex.quote(s)


def linux_commands(node: Node, sha: str) -> tuple[str, str, str]:
    r=q(node.repo_dir); s=q(node.service); rev=q(sha)
    pre=f"cd {r} && test -z \"$(git status --porcelain --untracked-files=no)\" && git rev-parse HEAD"
    deploy=(f"cd {r} && git fetch origin && git cat-file -e {rev}^{{commit}} && git checkout --detach {rev} && "
            f"./.venv/bin/pip install --quiet -e . && systemctl --user restart {s}")
    rollback=(f"cd {r} && git checkout --detach {{previous}} && ./.venv/bin/pip install --quiet -e . && "
              f"systemctl --user restart {s}")
    return pre, deploy, rollback


def windows_commands(node: Node, sha: str) -> tuple[str, str, str]:
    r=node.repo_dir.replace("'", "''"); rev=sha.replace("'", "''")
    # Installer uses a Scheduled Task; Stop/Start by task name is safer than killing sessions.
    task=node.service.replace("'", "''")
    pre=f"powershell -NoProfile -Command \"Set-Location '{r}'; if (git status --porcelain --untracked-files=no) {{ exit 42 }}; git rev-parse HEAD\""
    deploy=(f"powershell -NoProfile -Command \"Set-Location '{r}'; git fetch origin; git cat-file -e '{rev}^{{commit}}'; "
            f"if ($LASTEXITCODE) {{ exit $LASTEXITCODE }}; git checkout --detach '{rev}'; & .\\.venv\\Scripts\\pip.exe install --quiet -e .; "
            f"Stop-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; Start-ScheduledTask -TaskName '{task}'\"")
    rollback=(f"powershell -NoProfile -Command \"Set-Location '{r}'; git checkout --detach '{{previous}}'; "
              f"& .\\.venv\\Scripts\\pip.exe install --quiet -e .; Stop-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; Start-ScheduledTask -TaskName '{task}'\"")
    return pre, deploy, rollback


def commands(node, sha): return windows_commands(node, sha) if node.platform.lower()=="windows" else linux_commands(node, sha)


def health(node: Node, sha: str) -> tuple[bool,str]:
    if not node.health_url: return True, "health_url not configured"
    try:
        with urllib.request.urlopen(node.health_url.rstrip('/')+"/v1/health", timeout=8) as resp:
            body=json.loads(resp.read().decode())
        version=body.get("agent_version") or "?"
        return resp.status==200, f"http={resp.status} agent_version={version}"
    except Exception as e: return False, f"{type(e).__name__}: {e}"


def load(path: Path) -> list[Node]:
    raw=json.loads(path.read_text())
    return [Node(**x) for x in raw["nodes"] if x.get("enabled", True)]


def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--manifest", required=True); p.add_argument("--revision", required=True)
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--node", action="append"); a=p.parse_args()
    # Resolve once on controller: every node receives the same immutable full SHA.
    sha=run(["git","rev-parse",f"{a.revision}^{{commit}}"], check=True).stdout.strip()
    nodes=load(Path(a.manifest)); wanted=set(a.node or [])
    if wanted: nodes=[n for n in nodes if n.node_id in wanted]
    print(f"desired_revision={sha} nodes={','.join(n.node_id for n in nodes)}")
    for n in nodes:
        pre,deploy,rollback=commands(n,sha)
        if a.dry_run:
            print(f"DRY_RUN {n.node_id} platform={n.platform} ssh={n.ssh} health={n.health_url or '-'}")
            continue
        print(f"[{n.node_id}] preflight", flush=True)
        r=ssh(n,pre,check=False)
        if r.returncode:
            print(f"[{n.node_id}] BLOCKED preflight rc={r.returncode}: {(r.stderr or r.stdout).strip()}", file=sys.stderr); return 2
        previous=r.stdout.strip().splitlines()[-1]
        print(f"[{n.node_id}] {previous[:12]} -> {sha[:12]}", flush=True)
        d=ssh(n,deploy,check=False)
        if d.returncode:
            print(f"[{n.node_id}] deploy failed rc={d.returncode}; rolling back", file=sys.stderr)
            ssh(n,rollback.format(previous=previous),check=False); return 3
        ok,detail=health(n,sha); print(f"[{n.node_id}] health {detail}")
        if not ok:
            print(f"[{n.node_id}] health failed; rolling back to {previous[:12]}", file=sys.stderr)
            ssh(n,rollback.format(previous=previous),check=False); return 4
        # Verify the checkout itself after restart; health proves process liveness.
        got=ssh(n,f"cd {q(n.repo_dir)} && git rev-parse HEAD",check=False)
        if got.returncode or got.stdout.strip()!=sha:
            print(f"[{n.node_id}] revision mismatch; rolling back", file=sys.stderr)
            ssh(n,rollback.format(previous=previous),check=False); return 5
        print(f"[{n.node_id}] CONVERGED {sha[:12]}")
    print(f"FLEET_CONVERGED {sha}"); return 0
if __name__ == "__main__": raise SystemExit(main())
