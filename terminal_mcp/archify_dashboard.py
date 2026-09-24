"""Standalone dashboard page and thin HTTP routes for Archify jobs."""

from __future__ import annotations

from typing import Any, Callable

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .archify_service import ArchifyService, ArchifyServiceError


ARCHIFY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Archify · Terminal MCP</title>
<style>
:root{color-scheme:dark;--bg:#080d18;--panel:#101827;--line:#26344d;--muted:#94a3b8;--text:#e5edf8;--accent:#6ea8fe;--ok:#4ade80;--bad:#fb7185}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px ui-sans-serif,system-ui,sans-serif}
.wrap{max-width:1500px;margin:auto;padding:22px}.hero{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:18px}
h1{font-size:25px;margin:0 0 5px}.sub{color:var(--muted);margin:0}.state{border:1px solid var(--line);border-radius:999px;padding:7px 11px;color:var(--muted)}
.state.ready{color:var(--ok);border-color:#245c3a}.state.bad{color:var(--bad);border-color:#653342}.grid{display:grid;grid-template-columns:minmax(300px,390px) 1fr;gap:18px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}.panel h2{font-size:15px;margin:0 0 13px}
label{display:block;color:var(--muted);font-size:12px;margin:11px 0 5px}select,textarea,button{font:inherit}select,textarea{width:100%;color:var(--text);background:#0b1220;border:1px solid var(--line);border-radius:8px;padding:9px}
textarea{min-height:100px;resize:vertical}.actions{display:flex;gap:8px;margin-top:12px}button,.open{border:1px solid var(--line);background:#17233a;color:var(--text);border-radius:8px;padding:8px 11px;cursor:pointer;text-decoration:none}
button.primary{background:#2459a9;border-color:#3979d4}button:disabled{opacity:.45;cursor:not-allowed}.message{min-height:20px;color:var(--muted);margin-top:10px;font-size:12px}.message.error{color:var(--bad)}
.history{margin-top:18px}.jobs{display:grid;gap:8px}.job{display:grid;grid-template-columns:1fr auto;gap:8px;padding:11px;border:1px solid var(--line);border-radius:9px;background:#0b1220}.job-title{font-weight:650}.meta,.error{font-size:12px;color:var(--muted);margin-top:4px}.error{color:var(--bad)}.job-actions{display:flex;gap:6px;align-items:center}.status{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.preview-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px}.preview-head h2{margin:0}.preview{width:100%;height:min(72vh,900px);border:1px solid var(--line);border-radius:9px;background:white}.empty{color:var(--muted);padding:32px;text-align:center;border:1px dashed var(--line);border-radius:9px}
@media(max-width:900px){.wrap{padding:14px}.hero{display:block}.state{display:inline-block;margin-top:10px}.grid{grid-template-columns:1fr}.preview{height:62vh}}
</style></head><body><main class="wrap">
<div class="hero"><div><h1>Archify</h1><p class="sub">Source-backed diagrams, persisted as durable Terminal MCP jobs.</p></div><div id="runtimeState" class="state">Checking shared runtime…</div></div>
<div class="grid"><section><div class="panel"><h2>Generate diagram</h2>
<form id="archifyForm"><label for="project">Allowed project</label><select id="project" required></select>
<label for="diagramType">Diagram type</label><select id="diagramType"><option value="architecture">Architecture</option><option value="workflow">Workflow</option><option value="sequence">Sequence</option><option value="dataflow">Data flow</option><option value="lifecycle">Lifecycle</option></select>
<label for="prompt">Optional focus</label><textarea id="prompt" placeholder="Example: focus on dashboard, API, and durable task flow"></textarea>
<div class="actions"><button id="generateBtn" class="primary" type="submit">Generate</button><button id="refreshBtn" type="button">Refresh</button></div><div id="message" class="message" role="status"></div></form></div>
<div class="panel history"><h2>Recent diagrams</h2><div id="jobs" class="jobs"></div></div></section>
<section class="panel"><div class="preview-head"><h2 id="previewTitle">Preview</h2><a id="openHtml" class="open" target="_blank" rel="noopener" hidden>Open HTML</a></div><div id="previewEmpty" class="empty">Choose a completed diagram from history.</div><iframe id="previewFrame" class="preview" title="Archify diagram preview" sandbox="allow-scripts" hidden></iframe></section></div>
</main><script>
const TYPES=new Set(['architecture','workflow','sequence','dataflow','lifecycle']);
const $=id=>document.getElementById(id);let state={ready:false,jobs:[]},pollTimer=null;
async function api(url,opts){const response=await fetch(url,opts);let body={};try{body=await response.json()}catch{}if(!response.ok)throw new Error(body.message||body.error||`HTTP ${response.status}`);return body}
function setMessage(text,error=false){$('message').textContent=text||'';$('message').classList.toggle('error',error)}
function renderStatus(status){state.ready=Boolean(status.ready);const node=$('runtimeState');node.textContent=status.runtime?.message||'Archify unavailable';node.className='state '+(state.ready?'ready':'bad');$('generateBtn').disabled=!state.ready||!$('project').value}
function renderProjects(projects){const select=$('project');const prior=select.value;select.replaceChildren();for(const item of projects){const option=document.createElement('option');option.value=item.path;option.textContent=`${item.name} — ${item.path}`;select.append(option)}if(prior)select.value=prior;$('generateBtn').disabled=!state.ready||!select.value}
function showJob(job){if(job.status!=='completed')return;const url=`/dashboard/api/archify/jobs/${encodeURIComponent(job.id)}/html`;$('previewFrame').src=url;$('previewFrame').hidden=false;$('previewEmpty').hidden=true;$('openHtml').href=url;$('openHtml').hidden=false;$('previewTitle').textContent=`${job.project_name} · ${job.diagram_type}`}
function renderJobs(jobs){state.jobs=jobs;const root=$('jobs');root.replaceChildren();if(!jobs.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='No Archify jobs yet.';root.append(empty);return}for(const job of jobs){const row=document.createElement('article');row.className='job';const info=document.createElement('div');const title=document.createElement('div');title.className='job-title';title.textContent=`${job.project_name} · ${job.diagram_type}`;const meta=document.createElement('div');meta.className='meta';meta.textContent=`${job.status} · ${new Date(job.created_at).toLocaleString()}`;info.append(title,meta);if(job.error_message){const error=document.createElement('div');error.className='error';error.textContent=job.error_message;info.append(error)}const actions=document.createElement('div');actions.className='job-actions';const status=document.createElement('span');status.className='status';status.textContent=job.status;actions.append(status);if(job.status==='completed'){const view=document.createElement('button');view.type='button';view.textContent='Preview';view.onclick=()=>showJob(job);actions.append(view)}row.append(info,actions);root.append(row)}}
async function load(){try{const [status,projects,jobs]=await Promise.all([api('/dashboard/api/archify/status'),api('/dashboard/api/archify/projects'),api('/dashboard/api/archify/jobs')]);renderStatus(status);renderProjects(projects.projects);renderJobs(jobs.jobs);const active=jobs.jobs.some(job=>job.status==='queued'||job.status==='running');clearTimeout(pollTimer);if(active)pollTimer=setTimeout(load,1000)}catch(error){setMessage(error.message,true)}}
$('archifyForm').addEventListener('submit',async event=>{event.preventDefault();setMessage('Queueing durable job…');$('generateBtn').disabled=true;try{const body={project:$('project').value,diagram_type:$('diagramType').value,prompt:$('prompt').value};if(!TYPES.has(body.diagram_type))throw new Error('Invalid diagram type');const result=await api('/dashboard/api/archify/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});setMessage(`Job ${result.job.id} queued.`);await load()}catch(error){setMessage(error.message,true);$('generateBtn').disabled=!state.ready}});
$('refreshBtn').onclick=load;$('project').onchange=()=>$('generateBtn').disabled=!state.ready||!$('project').value;load();
</script></body></html>"""


ERROR_STATUS = {
    "INVALID_REQUEST": 400,
    "INVALID_DIAGRAM_TYPE": 400,
    "PROMPT_TOO_LONG": 400,
    "PROJECT_NOT_ALLOWED": 403,
    "PROJECT_NOT_FOUND": 404,
    "JOB_NOT_FOUND": 404,
    "ARTIFACT_MISSING": 404,
    "ARCHIFY_DISABLED": 503,
    "ARCHIFY_UNAVAILABLE": 503,
}


def _error(exc: ArchifyServiceError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message},
                        status_code=ERROR_STATUS.get(exc.code, 500),
                        headers={"Cache-Control": "no-store"})


def register_archify_dashboard(
    server: MCPServer,
    service: ArchifyService,
    *,
    read_guard: Callable[[Request], tuple[Response | None, Any]],
    mutation_guard: Callable[[Request], tuple[Response | None, Any]],
    nav_page: Callable[[str], str],
) -> None:
    @server.custom_route("/dashboard/archify", methods=["GET"], include_in_schema=False)
    async def archify_page(request: Request) -> Response:
        blocked, _identity = read_guard(request)
        if blocked is not None:
            return blocked
        return HTMLResponse(nav_page(ARCHIFY_HTML), headers={
            "Cache-Control": "no-store", "X-Frame-Options": "DENY",
        })

    @server.custom_route("/dashboard/api/archify/status", methods=["GET"], include_in_schema=False)
    async def archify_status(request: Request) -> Response:
        blocked, _identity = read_guard(request)
        if blocked is not None:
            return blocked
        result = await anyio.to_thread.run_sync(service.status)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/archify/projects", methods=["GET"], include_in_schema=False)
    async def archify_projects(request: Request) -> Response:
        blocked, _identity = read_guard(request)
        if blocked is not None:
            return blocked
        projects = await anyio.to_thread.run_sync(service.projects)
        return JSONResponse({"projects": projects}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/archify/jobs", methods=["GET"], include_in_schema=False)
    async def archify_jobs(request: Request) -> Response:
        blocked, _identity = read_guard(request)
        if blocked is not None:
            return blocked
        raw_limit = request.query_params.get("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else None
        except ValueError:
            return JSONResponse({"error": "INVALID_REQUEST", "message": "limit must be an integer"},
                                status_code=400, headers={"Cache-Control": "no-store"})
        jobs = await anyio.to_thread.run_sync(lambda: service.list_jobs(limit=limit))
        return JSONResponse({"jobs": jobs}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/archify/jobs", methods=["POST"], include_in_schema=False)
    async def archify_create(request: Request) -> Response:
        blocked, _identity = mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "INVALID_REQUEST", "message": "JSON object required"},
                                status_code=400)
        try:
            job = await anyio.to_thread.run_sync(lambda: service.create_job(
                body.get("project", ""), body.get("diagram_type", ""), body.get("prompt", "")))
        except ArchifyServiceError as exc:
            return _error(exc)
        return JSONResponse({"job": job}, status_code=202, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/archify/jobs/{job_id}/html", methods=["GET"], include_in_schema=False)
    async def archify_html(request: Request) -> Response:
        blocked, _identity = read_guard(request)
        if blocked is not None:
            return blocked
        try:
            path = await anyio.to_thread.run_sync(
                lambda: service.html_path(request.path_params["job_id"]))
            content = await anyio.to_thread.run_sync(path.read_bytes)
        except ArchifyServiceError as exc:
            return _error(exc)
        return Response(content, media_type="text/html", headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox allow-scripts",
            "X-Content-Type-Options": "nosniff",
        })

    @server.custom_route("/dashboard/api/archify/jobs/{job_id}", methods=["GET"], include_in_schema=False)
    async def archify_job(request: Request) -> Response:
        blocked, _identity = read_guard(request)
        if blocked is not None:
            return blocked
        try:
            job = await anyio.to_thread.run_sync(
                lambda: service.get_job(request.path_params["job_id"]))
        except ArchifyServiceError as exc:
            return _error(exc)
        return JSONResponse({"job": job}, headers={"Cache-Control": "no-store"})
