"""Small, guarded Dispatch Settings dashboard for the NovaRetail feeder."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

CONFIG = Path.home() / ".config/novaretail/dispatch.json"
STATE = Path.home() / ".local/state/novaretail/dispatch_watchdog.jsonl"
ALLOWED_KEYS = {"project_id", "dispatcher_session", "workers", "mode", "allowed_roots", "bindings"}
DEFAULTS = {
    "project_id": "novaretail",
    "dispatcher_session": "codex1",
    "workers": [],
    "mode": "SINGLE_PROJECT",
    "allowed_roots": [],
    "bindings": {},
}


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    merged = {**DEFAULTS, **{k: raw[k] for k in ALLOWED_KEYS if k in raw}}
    try:
        return validate_config(merged)
    except ValueError:
        return dict(DEFAULTS)


def validate_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    unknown = set(payload) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    value = {**DEFAULTS, **payload}
    if value["project_id"] != "novaretail":
        raise ValueError("project_id must be novaretail")
    if value["mode"] not in {"SINGLE_PROJECT", "MULTI_PROJECT"}:
        raise ValueError("invalid mode")
    dispatcher = value["dispatcher_session"]
    if not isinstance(dispatcher, str) or not dispatcher.strip():
        raise ValueError("dispatcher_session must be a non-empty string")
    workers = value["workers"]
    if not isinstance(workers, list) or any(not isinstance(x, str) or not x.strip() for x in workers):
        raise ValueError("workers must be non-empty strings")
    if len(set(workers)) != len(workers):
        raise ValueError("workers must be unique")
    if dispatcher in workers:
        raise ValueError("dispatcher_session cannot be a worker")
    roots = value["allowed_roots"]
    if not isinstance(roots, list) or any(not isinstance(x, str) or not x.startswith("/") or "\x00" in x for x in roots):
        raise ValueError("allowed_roots must be absolute strings")
    bindings = value["bindings"]
    if not isinstance(bindings, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in bindings.items()):
        raise ValueError("bindings must be string to string")
    return {"project_id": "novaretail", "dispatcher_session": dispatcher.strip(),
            "workers": list(workers), "mode": value["mode"], "allowed_roots": list(roots),
            "bindings": dict(bindings)}


def save_config_atomic(cfg: dict[str, Any], path: Path = CONFIG) -> None:
    normalized = validate_config(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        pass
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(name, mode)
        else:
            os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def session_candidates(cfg: dict[str, Any], state: Path = STATE) -> list[dict[str, Any]]:
    names = {cfg["dispatcher_session"], *cfg["workers"]}
    latest: dict[str, dict[str, Any]] = {}
    try:
        for line in state.read_text(encoding="utf-8").splitlines()[-100:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            for item in event.get("worker_classification", []) if isinstance(event, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("session"), str):
                    latest[item["session"]] = item
                    names.add(item["session"])
    except (FileNotFoundError, OSError):
        pass
    result = []
    for name in sorted(names):
        item = latest.get(name, {})
        result.append({"session": name, "state": item.get("eligibility_reason", "UNKNOWN"),
                       "cwd": item.get("cwd", ""), "project_match": item.get("project_match"),
                       "eligibility_reason": item.get("eligibility_reason", "CONFIGURED")})
    return result


HTML = """<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><title>Dispatch Settings</title>
<style>body{font:14px system-ui;background:#0b1020;color:#eef2ff;margin:0}.w{max-width:900px;margin:auto;padding:20px}.c{border:1px solid #26324b;border-radius:9px;padding:14px;background:#121a2d;margin:10px 0}label{display:block;margin:10px 0}input,select,textarea{width:100%;box-sizing:border-box;padding:8px;background:#0e1628;color:#eef2ff;border:1px solid #405070;border-radius:5px}button,a{padding:8px 12px;background:#5b8cff;color:#07101f;border:0;border-radius:6px;text-decoration:none;margin:4px}.list{display:flex;flex-wrap:wrap;gap:6px}.pill{border:1px solid #405070;padding:6px;border-radius:6px}</style>
<main class=w><h1>Dispatch Settings</h1><div class=c><label>Project<select id=project><option value=novaretail>novaretail</option></select></label>
<label>Dispatcher<select id=dispatcher></select></label><label>Mode<select id=mode><option>SINGLE_PROJECT</option><option>MULTI_PROJECT</option></select></label>
<h3>Available sessions</h3><div id=sessions class=list></div><h3>Workers</h3><div id=workers class=list></div>
<label>Allowed roots (one per line)<textarea id=roots rows=3></textarea></label><label>Bindings (JSON object)<textarea id=bindings rows=3>{}</textarea></label>
<button id=save>Save</button><button id=reset>Reset</button><a href='/dashboard/ops/novaretail-dispatch'>Back to Monitor</a><output id=msg></output></div></main>
<script>const $=id=>document.getElementById(id);let data;function esc(x){return String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}function render(){let c=data.config;project.value=c.project_id;mode.value=c.mode;dispatcher.innerHTML=data.sessions.map(x=>`<option>${esc(x.session)}</option>`).join('');dispatcher.value=c.dispatcher_session;roots.value=c.allowed_roots.join('\n');bindings.value=JSON.stringify(c.bindings,null,2);workers.innerHTML=c.workers.map(x=>`<span class=pill>${esc(x)} <button data-r='${esc(x)}'>Remove</button></span>`).join('');sessions.innerHTML=data.sessions.map(x=>`<span class=pill>${esc(x.session)} <button data-a='${esc(x.session)}'>Add</button></span>`).join('');document.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>{if(!data.config.workers.includes(b.dataset.a)&&b.dataset.a!==dispatcher.value)data.config.workers.push(b.dataset.a);render()});document.querySelectorAll('[data-r]').forEach(b=>b.onclick=()=>{data.config.workers=data.config.workers.filter(x=>x!==b.dataset.r);render()})}async function load(){let r=await fetch('/dashboard/api/ops/dispatch-settings?project=novaretail');data=await r.json();render()}save.onclick=async()=>{try{let r=await fetch('/dashboard/api/ops/dispatch-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:project.value,dispatcher_session:dispatcher.value,workers:data.config.workers,mode:mode.value,allowed_roots:roots.value.split('\n').map(x=>x.trim()).filter(Boolean),bindings:JSON.parse(bindings.value||'{}')})});let j=await r.json();msg.textContent=r.ok?'Saved for next watchdog tick':(j.error||'Save failed');if(r.ok){data.config=j.config;render()}}catch(e){msg.textContent=e}};reset.onclick=load;load()</script>"""


def register(server, read_guard, mutation_guard, config_path: Path = CONFIG):
    @server.custom_route("/dashboard/ops/dispatch-settings", methods=["GET"], include_in_schema=False)
    async def page(request: Request):
        blocked, _ = read_guard(request)
        return blocked or HTMLResponse(HTML)

    @server.custom_route("/dashboard/api/ops/dispatch-settings", methods=["GET"], include_in_schema=False)
    async def api(request: Request):
        blocked, _ = read_guard(request)
        if blocked:
            return blocked
        cfg = load_config(config_path)
        return JSONResponse({"config": cfg, "sessions": session_candidates(cfg)})

    @server.custom_route("/dashboard/api/ops/dispatch-settings", methods=["POST"], include_in_schema=False)
    async def save(request: Request):
        blocked, _ = mutation_guard(request)
        if blocked:
            return blocked
        try:
            payload = await request.json()
            cfg = validate_config(payload)
            save_config_atomic(cfg, config_path)
            return JSONResponse({"saved": True, "config": cfg})
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
