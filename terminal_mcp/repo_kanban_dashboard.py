"""Dashboard surface for repo_kanban.py: one page, two read-only JSON routes.

    GET /dashboard/repo-kanban                      page (list, or ?repo=<id> board)
    GET /dashboard/api/repo-kanban/projects         every configured repo + counts
    GET /dashboard/api/repo-kanban/board?repo=<id>  one repo's cards

All three sit behind the dashboard's _read_guard (Cloudflare Access when
configured), exactly like /dashboard/projects. There is no POST: the board is
changed by committing to the repo, never from here. Every value on the page is
agent-written text, so the script builds DOM with textContent only.
"""
from __future__ import annotations

from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import dashboard_nav
from .repo_kanban import RepoKanbanError, RepoKanbanService

_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY"}
_ERROR_STATUS = {"REPO_NOT_FOUND": 404, "REPO_KANBAN_DISABLED": 404}

HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Repo Kanban</title>
<style>
:root{--bg:#f6f5f1;--panel:#fff;--ink:#1d1d1b;--muted:#6b6a64;--line:#e2e0d8;--claude:#c96442;--codex:#2f6fde;--owner:#7a4fc0;--external:#5d6b5f;--ok:#2e7d4f;--bad:#b3261e;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#171716;--panel:#22221f;--ink:#ecebe6;--muted:#a3a29b;--line:#383732;--ok:#6fcf97;--bad:#ff8a80;--warn:#e3b341}}
:root[data-theme="dark"]{--bg:#171716;--panel:#22221f;--ink:#ecebe6;--muted:#a3a29b;--line:#383732;--ok:#6fcf97;--bad:#ff8a80;--warn:#e3b341}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1600px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 8px}
.meta{color:var(--muted);margin:0 0 12px}.muted{color:var(--muted)}
.projects{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.project,.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}
.project h2 a{color:inherit}.counts{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.count{border:1px solid var(--line);border-radius:4px;padding:2px 6px;font-size:12px}
.err{color:var(--bad)}.warn{color:var(--warn);font-size:12px;margin:4px 0}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}.filters button{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:999px;padding:4px 12px;cursor:pointer}
.filters button[aria-pressed="true"]{background:var(--ink);color:var(--bg)}
.board{display:grid;grid-template-columns:repeat(5,minmax(230px,1fr));gap:12px}
@media (max-width:900px){.board{grid-template-columns:1fr}}
.col small{color:var(--muted);font-weight:400}.card{margin:0 0 10px;padding:10px}.card p{margin:6px 0}
.card header{display:flex;justify-content:space-between;gap:8px}.title{font-weight:500}
.small{font-size:12px}.label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase}
.owner{font-size:11px;color:#fff;border-radius:4px;padding:1px 6px;background:var(--external)}
.owner-Claude{background:var(--claude)}.owner-Codex{background:var(--codex)}.owner-Owner{background:var(--owner)}
.chip{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:4px;padding:1px 6px;margin:0 4px 4px 0;color:var(--ink);text-decoration:none}
.ok{border-color:var(--ok);color:var(--ok)}.bad{border-color:var(--bad);color:var(--bad)}code{font-size:12px;word-break:break-all}
table{border-collapse:collapse;font-size:12px}td,th{border-top:1px solid var(--line);padding:4px 8px;text-align:left}
</style></head><body><main>
<h1 id="heading">Repo Kanban</h1>
<p class="meta" id="meta">Đang tải…</p>
<div id="filters" class="filters" role="group" aria-label="Lọc theo owner" hidden></div>
<div id="content"></div>
</main><script>
(function () {
  'use strict';
  var COLUMN_TITLES = { RUNNING: 'Đang chạy', NEW: 'Mới', REVIEW: 'Chờ review / merge', BLOCKED: 'Bị chặn', DONE: 'Xong' };
  var QA_TITLES = { AT_HEAD: 'PASS @ head', DOCS_AHEAD: 'PASS (chỉ thêm docs)', STALE: 'QA cũ', FAIL: 'QA FAIL', NONE: 'chưa QA' };
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === 'text') node.textContent = attrs[key];
      else if (key === 'className') node.className = attrs[key];
      else node.setAttribute(key, attrs[key]);
    });
    (children || []).forEach(function (child) { if (child) node.append(child); });
    return node;
  }
  function safeHref(url) { return /^https:\\/\\/github\\.com\\//.test(url || '') || /^\\/dashboard\\//.test(url || '') ? url : '#'; }
  async function api(path) {
    var response = await fetch(path, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    var body = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error((body && body.error) || ('HTTP ' + response.status));
    return body;
  }
  var params = new URLSearchParams(location.search);
  var content = document.getElementById('content');
  var meta = document.getElementById('meta');

  function countsRow(project) {
    var row = el('div', { className: 'counts' });
    Object.keys(COLUMN_TITLES).forEach(function (column) {
      var byOwner = project.counts[column] || {};
      var total = Object.keys(byOwner).reduce(function (sum, owner) { return sum + byOwner[owner]; }, 0);
      var detail = Object.keys(byOwner).filter(function (owner) { return byOwner[owner]; })
        .map(function (owner) { return owner + ' ' + byOwner[owner]; }).join(', ');
      row.append(el('span', { className: 'count', title: detail, text: column + ' ' + total }));
    });
    return row;
  }

  async function renderList() {
    var body = await api('/dashboard/api/repo-kanban/projects');
    meta.textContent = body.enabled === false ? 'repo_kanban đang tắt trong config.'
      : body.count + ' repo · đọc từ docs/kanban/board.json + live.json đã commit · ' + body.generated_at;
    if (!body.projects.length) { content.append(el('p', { className: 'muted', text: 'Chưa cấu hình repo nào (repo_kanban.repos).' })); return; }
    var grid = el('div', { className: 'projects' });
    body.projects.forEach(function (project) {
      var card = el('section', { className: 'project', 'data-repo': project.id });
      card.append(el('h2', {}, [el('a', { href: '/dashboard/repo-kanban?repo=' + encodeURIComponent(project.id), text: project.name })]));
      card.append(el('p', { className: 'small muted', text: (project.remote || 'không có remote') + ' · ref ' + project.ref + (project.ref_sha ? ' @ ' + project.ref_sha.slice(0, 7) : '') }));
      if (project.error) card.append(el('p', { className: 'err', text: project.error.code + ': ' + project.error.message }));
      else card.append(countsRow(project));
      var facts = [];
      if (project.integration && project.integration.branch) facts.push('tích hợp ' + project.integration.branch + (project.integration.sha ? ' @ ' + project.integration.sha.slice(0, 7) : '') + (project.integration.qa ? ' · QA ' + (QA_TITLES[project.integration.qa] || project.integration.qa) : ''));
      facts.push('session ' + project.sessions.active + ' active / ' + project.sessions.total);
      if (project.live_generated_at) facts.push('sync ' + project.live_generated_at + (project.live_generated_by ? ' (' + project.live_generated_by + ')' : ''));
      facts.push('cập nhật ' + (project.last_updated || '—'));
      card.append(el('p', { className: 'small', text: facts.join(' · ') }));
      if (project.warnings) card.append(el('p', { className: 'warn', text: '⚠ ' + project.warnings + ' cảnh báo lệch trạng thái' }));
      var links = el('p', { className: 'small' });
      links.append(el('a', { href: '/dashboard/repo-kanban?repo=' + encodeURIComponent(project.id), text: 'Mở board' }));
      if (project.project) { links.append(' · '); links.append(el('a', { href: '/dashboard/projects?project_id=' + encodeURIComponent(project.project), text: 'Project' })); }
      if (project.github_url) { links.append(' · '); links.append(el('a', { href: safeHref(project.github_url), rel: 'noopener', text: 'GitHub' })); }
      card.append(links);
      grid.append(card);
    });
    content.append(grid);
  }

  function cardNode(card, githubUrl) {
    var node = el('article', { className: 'card', 'data-owner': card.owner, 'data-card': card.id });
    node.append(el('header', {}, [el('b', { text: card.id }), el('span', { className: 'owner owner-' + card.owner, text: card.owner })]));
    node.append(el('p', { className: 'title', text: card.title }));
    if (card.branch) {
      var branch = el('p', { className: 'small' }, [el('code', { text: card.branch })]);
      if (card.branch_live && card.branch_live.sha) branch.append(' @ ', el('code', { text: card.branch_live.sha.slice(0, 7) }));
      if (card.branch_live && card.branch_live.dirtyWorktrees) branch.append(el('span', { title: 'worktree chưa commit', text: ' ✎' }));
      node.append(branch);
    }
    var chips = el('p', {});
    card.prs.forEach(function (pr) {
      chips.append(el('a', { className: 'chip' + (pr.state === 'MERGED' ? ' ok' : pr.state === 'CLOSED' ? ' bad' : ''), href: safeHref(githubUrl ? githubUrl + '/pull/' + pr.number : '#'), rel: 'noopener',
        text: 'PR #' + pr.number + ' ' + (pr.state === 'OPEN' && pr.draft ? 'DRAFT' : pr.state) }));
    });
    card.issues.forEach(function (issue) {
      chips.append(el('a', { className: 'chip', href: safeHref(githubUrl ? githubUrl + '/issues/' + issue.number : '#'), rel: 'noopener', text: '#' + issue.number + (issue.state === 'CLOSED' ? ' closed' : '') }));
    });
    if (card.branch_live && card.branch_live.qa) {
      var qa = card.branch_live.qa.status;
      chips.append(el('span', { className: 'chip ' + (qa === 'AT_HEAD' || qa === 'DOCS_AHEAD' ? 'ok' : qa === 'NONE' ? '' : 'bad'), title: card.branch_live.qa.run || '', text: 'QA: ' + (QA_TITLES[qa] || qa) }));
    }
    card.sessions.forEach(function (session) {
      chips.append(el('span', { className: 'chip', title: session.node_id + ' · ' + session.status + ' · ' + session.last_activity_at, text: '⌁ ' + session.session_name + ' (' + (session.agent_type || '?') + ')' }));
    });
    node.append(chips);
    if (card.evidence) node.append(el('p', { className: 'small' }, [el('span', { className: 'label', text: 'Bằng chứng' }), card.evidence]));
    node.append(el('p', { className: 'small' }, [el('span', { className: 'label', text: 'Bước tiếp' }), card.next || '—']));
    card.warnings.forEach(function (warning) { node.append(el('p', { className: 'warn', text: '⚠ ' + warning })); });
    node.append(el('p', { className: 'small muted', text: 'Cập nhật ' + (card.last_updated || '—') }));
    return node;
  }

  async function renderBoard(repoId) {
    var body = await api('/dashboard/api/repo-kanban/board?repo=' + encodeURIComponent(repoId));
    var project = body.project;
    document.getElementById('heading').textContent = project.name + ' — Kanban';
    document.title = project.name + ' Kanban';
    if (window.tmcpBreadcrumb) window.tmcpBreadcrumb([{ label: 'Repo Kanban', href: '/dashboard/repo-kanban' }, { label: project.name }]);
    meta.textContent = '';
    meta.append(el('a', { href: '/dashboard/repo-kanban', text: '← Tất cả repo' }), ' · ' + (project.remote || '') + ' · ref ' + project.ref + (project.ref_sha ? ' @ ' + project.ref_sha.slice(0, 7) : '')
      + (project.live_generated_at ? ' · sync ' + project.live_generated_at : ' · chưa có live.json') + ' · thẻ ' + (project.board_updated || '—'));
    if (project.error) { content.append(el('p', { className: 'err', text: project.error.code + ': ' + project.error.message })); return; }
    if (body.rules.length) content.append(el('p', { className: 'small', text: 'Luật cứng: ' + body.rules.join(' · ') }));
    var filters = document.getElementById('filters');
    filters.hidden = false;
    ['', 'Claude', 'Codex', 'Owner', 'External'].forEach(function (owner) {
      var button = el('button', { type: 'button', 'aria-pressed': owner ? 'false' : 'true', text: owner || 'Tất cả' });
      button.addEventListener('click', function () {
        filters.querySelectorAll('button').forEach(function (other) { other.setAttribute('aria-pressed', String(other === button)); });
        content.querySelectorAll('.card').forEach(function (card) { card.hidden = Boolean(owner) && card.dataset.owner !== owner; });
      });
      filters.append(button);
    });
    var board = el('div', { className: 'board' });
    body.columns.forEach(function (column) {
      var cards = body.cards.filter(function (card) { return card.status === column; });
      var col = el('section', { className: 'col', 'data-column': column }, [el('h2', { text: (COLUMN_TITLES[column] || column) + ' ' }, [el('small', { text: column + ' · ' + cards.length })])]);
      cards.forEach(function (card) { col.append(cardNode(card, project.github_url)); });
      if (!cards.length) col.append(el('p', { className: 'muted', text: '—' }));
      board.append(col);
    });
    content.append(board);
    if (body.sessions.length) {
      var table = el('table', {}, [el('tr', {}, ['Session', 'Node', 'Agent', 'Trạng thái', 'Nhánh', 'Hoạt động'].map(function (h) { return el('th', { text: h }); }))]);
      body.sessions.forEach(function (s) { table.append(el('tr', {}, [s.session_name, s.node_id, s.agent_type, s.status + (s.state ? ' / ' + s.state : ''), s.branch, s.last_activity_at].map(function (v) { return el('td', { text: v || '—' }); }))); });
      content.append(el('h2', { text: 'Session trên repo này' }), table);
    }
    if (body.qa_runs.length) {
      var runs = el('table', {}, [el('tr', {}, ['Run', 'Commit', 'Kết quả', 'Xong lúc'].map(function (h) { return el('th', { text: h }); }))]);
      body.qa_runs.forEach(function (r) { runs.append(el('tr', {}, [r.id, r.commit.slice(0, 7), r.passed ? 'PASS' : 'FAIL' + (r.failed_step ? ' (' + r.failed_step + ')' : ''), r.completed_at].map(function (v) { return el('td', { text: v || '—' }); }))); });
      content.append(el('h2', { text: 'Lượt QA gần nhất' }), runs);
    }
  }

  // /dashboard/projects links here with ?project=<slug>: open that repo's
  // board when one is configured for it, else the list with a note.
  async function start() {
    if (params.get('repo')) return renderBoard(params.get('repo'));
    if (params.get('project')) {
      var list = await api('/dashboard/api/repo-kanban/projects');
      var match = list.projects.filter(function (p) { return p.project === params.get('project'); })[0];
      if (match) { history.replaceState({}, '', '/dashboard/repo-kanban?repo=' + encodeURIComponent(match.id)); return renderBoard(match.id); }
      content.append(el('p', { className: 'warn', text: 'Project "' + params.get('project') + '" chưa gắn repo Kanban nào (repo_kanban.repos[].project).' }));
    }
    return renderList();
  }
  start().catch(function (error) {
    meta.textContent = '';
    content.append(el('p', { className: 'err', text: 'Không tải được: ' + error.message }));
  });
})();
</script></body></html>
"""


def build_repo_kanban_service(terminal: Any) -> RepoKanbanService:
    config = terminal.config
    roots = tuple(config.repo_read.allowed_roots or config.session_lifecycle.allowed_cwd_roots)
    registry = getattr(terminal, "session_registry", None)
    sessions = (lambda: registry.list(limit=2000)) if registry is not None else None
    return RepoKanbanService(config.repo_kanban, allowed_roots=roots, sessions=sessions)


def register(server: Any, read_guard: Any, service: RepoKanbanService) -> None:
    @server.custom_route("/dashboard/repo-kanban", methods=["GET"], include_in_schema=False)
    async def page(request: Request):
        blocked, _ = read_guard(request)
        return blocked or HTMLResponse(
            dashboard_nav.apply(HTML, dashboard_nav.DASHBOARD_NAV, "repo-kanban"), headers=_HEADERS)

    @server.custom_route("/dashboard/api/repo-kanban/projects", methods=["GET"], include_in_schema=False)
    async def projects(request: Request):
        blocked, _ = read_guard(request)
        if blocked is not None:
            return blocked
        result = await anyio.to_thread.run_sync(service.list_projects)
        return JSONResponse(result, headers=_HEADERS)

    @server.custom_route("/dashboard/api/repo-kanban/board", methods=["GET"], include_in_schema=False)
    async def board(request: Request):
        blocked, _ = read_guard(request)
        if blocked is not None:
            return blocked
        repo_id = request.query_params.get("repo") or ""
        try:
            result = await anyio.to_thread.run_sync(lambda: service.get_board(repo_id))
        except RepoKanbanError as exc:
            return JSONResponse({"error": exc.code, "message": exc.message},
                                status_code=_ERROR_STATUS.get(exc.code, 400), headers=_HEADERS)
        return JSONResponse(result, headers=_HEADERS)
