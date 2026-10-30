# Macbook AMUX / Terminal MCP takeover audit (2026-10-30)

Supervisor task: dt_83c0358dda0f4e29. Evidence-only PR; unfinished board code is excluded.

## DONE: repository and ownership audit

All three requested macbook-claude sessions returned SESSION_DELETED. No historical completion or permissions were inferred. Current owner is macbook-codex-claude-amux-tmcp-20261030.

AMUX lives at /home/mac/workspace/amux-poc-20261023/source on main. Only crates/amux-server/src/lib.rs is dirty. verified-source is clean and detached. README.md supersedes PILOT-STATUS.md and its older Claude launch scripts.

Terminal MCP runtime at /home/mac/terminal-mcp is on issue80-live-canary with existing configuration/install changes and credential-like filenames. Only git status was read; those file contents were not accessed.

Task board at /home/mac/workspace/terminal-mcp-82 is on feat/82-repo-task-board-codex, base 4b9c44d. Four modified integration files and three untracked implementation/test files remain preserved. No PR existed for that head. This audit uses a separate docs worktree from fetched origin/main, also 4b9c44d. No reset, stash, clean or patch reversion was performed.

## DONE: AMUX listener and patch preservation

The local lib.rs patch defaults AMUX_BIND_IP to IPv4 loopback. It remains in place. Source-file SHA-256:
57c69e6d6d1cf1102eb61c99d1ddfa63620e39073c3af8974b40c768f10d1123

Observed runtime:
- amux-poc-server-20261024.service is active.
- ss -lnt for 8824/8822/18824 showed only 127.0.0.1:8824.
- Anonymous HTTPS requests with five-second bounds: /api/health 200; /api/workers 401; /api/config 404; / 200.
- Loopback probes disabled certificate verification; they do not validate trusted TLS or authenticated workflows.

The current runbook uses AMUX_LISTEN_FD handoff and records pinned CI binaries at commit 250366ede0a39e97c6298bef264c36f47353b972, manifest sha256:5447ae459cb59fbf6cbd194dff65677a0d3006b8126f870c1fa595956ef16ff9. Provenance was read from the runbook, not independently downloaded again. The earlier local build OOM at 5 GiB remains a build limitation; cargo check is not binary-build PASS. No service restart, installer, rebuild or resource increase was attempted.

## DONE: narrow Macbook network/auth checks

The macbook Linux fleet node is online; the separate mac17-macbook-pro macOS node is offline. GitHub DNS resolution, gh api user (hungtranbkit), issue and PR reads succeeded. This does not prove every node credential or Claude/Codex subscription is healthy. No environment dump, credential file or cookie store was requested.

## BLOCKED: board delivery, issue #82 remains open

[Issue #82](https://github.com/hungtranbkit/terminal-mcp/issues/82) explicitly requests a draft PR and no production deployment. The preserved implementation is read-only P0; Continue and Run QA are disabled. P1/P2 and full acceptance are unfinished.

Tests run in terminal-mcp-82:
- .venv/bin/python -m pytest tests/test_repo_task_board.py -q: 23 passed in 0.95s.
- The board, project_workflows, webauth_dashboard, dashboard_task_routing and dashboard_tab_ux_fixes test files together: 101 passed in 29.26s.
- Branch coverage run on test_repo_task_board.py: repo_task_board.py 66% (267 statements, 75 missed, 118 branches, 18 partial); repo_task_board_dashboard.py 100%; combined 70%.
- git diff --check: no errors. Ruff was unavailable.

Coverage was installed only in the local venv, with no manifest change. Coverage data was moved to /tmp/tmcp82-coverage-data. Test/coverage logs are /tmp/tmcp82-* local artifacts, not durable CI evidence.

The backend fails the applicable default 95% gate. The code was not committed, pushed or deployed. Next engineering work: repository-discovery/adapter and error-path tests, integration coverage, independent review, authenticated browser/mobile validation, restart/event reconciliation, guarded P1 binding/resume and real-repository P2 pilots. These are engineering gaps; human approval alone does not resolve them.

The separate HP terminal recovery lane was running session-deletion work and was not interrupted. Open PRs #92, #94, #95, #97, #98, #99, #101 and #102 were observed; no merge-readiness claim is made and no unrelated PR was changed.

## DONE: BrandAgent handoff; BLOCKED: live Facebook validation

Bounded depth-two workspace name discovery found no Facebook/BrandAgent repository on Macbook; this is not an exhaustive absence claim. The inspected BrandAgent Codex lane owns the separate /home/dell/workspace/brand-agent-studio-20261029 worktree, branch feat/brandagent-brand-studio-20261029.

Direct handoff to dell-codex-brandagent-ai-marketing-20261029 returned SUBMIT_CONFIRMED, submission 221955290e174d949bdbaf880816a3b9. This proves delivery only. The previous enqueue attempt returned QUEUE_DISABLED_USE_DIRECT_SESSION and created no task.

BrandAgent was asked to recover workflow/asset ownership from non-secret docs and use fixtures offline. Live validation requires owner-supplied workflow/account scope, appropriate Meta authorization, and an accessible legitimate test surface. The handoff grants no login, scraping, posting, messaging, cookie transfer, private-data access or checkpoint bypass.

[Meta guidance](https://about.fb.com/news/2021/04/how-we-combat-scraping/) says automated collection requires permission; this is platform guidance, not a legal determination for an unidentified workflow. The [current terms URL](https://www.facebook.com/legal/automated_data_collection_terms) redirected the unauthenticated web reader to login/temporary block. Current terms and logged-in operation were not verified; this does not show that the user account is blocked. No login or bypass was attempted.

## Overall

Audit/handoff and AMUX patch/listener preservation: DONE.
Board code delivery: BLOCKED by coverage and incomplete acceptance.
Facebook live verification: BLOCKED pending owner inputs above.
Do not close issue #82 or mark the full takeover complete.
