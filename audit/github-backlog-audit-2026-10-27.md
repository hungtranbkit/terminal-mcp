# hungtranbkit GitHub backlog audit

Baseline timestamp (UTC): 2026-10-27 14:48:40 UTC (enumeration completed approx. 14:49:01 UTC).

Repositories enumerated: **59** (owner/collaborator/organization_member affiliation, private included when visible to authenticated `gh` account). Source: `GitHub REST API, all pages; /user/repos affiliation=owner,collaborator,organization_member`. For each repository the audit fetched all pages of `/issues?state=open&per_page=100` and `/pulls?state=open&per_page=100`; issue entries with a `pull_request` field were excluded from issue counts. Baseline: **286 issues / 182 PRs**. Errors: none reported. The raw paginated snapshot is retained on the audit machine at `/tmp/hungtranbkit-open-backlog-baseline.json`; this Markdown table is the durable baseline.

## Per repository baseline

| Repository | Open issues | Open PRs | Archived |
|---|---:|---:|:---:|
| `hungtranbkit/ai-agent-cli` | 0 | 0 | — |
| `hungtranbkit/ai-design-council` | 0 | 0 | — |
| `hungtranbkit/ai-marketing-manager` | 0 | 1 | — |
| `hungtranbkit/ai-qc-agent` | 0 | 0 | — |
| `hungtranbkit/bizconnect` | 0 | 0 | — |
| `hungtranbkit/chatgpt-ai-gateway` | 19 | 18 | — |
| `hungtranbkit/chon-do-thong-minh-admin-ui` | 0 | 0 | — |
| `hungtranbkit/chon-do-thong-minh-platform` | 3 | 12 | — |
| `hungtranbkit/claude-wm-integration-push-test-1787963796` | 0 | 1 | — |
| `hungtranbkit/claude-wm-real-merge-test-1787932214` | 0 | 1 | — |
| `hungtranbkit/cloudflare-tunnel-publisher` | 1 | 0 | — |
| `hungtranbkit/contextai-guide` | 4 | 0 | — |
| `hungtranbkit/customer-ai-platform` | 0 | 0 | — |
| `hungtranbkit/docso-service` | 0 | 0 | — |
| `hungtranbkit/esp32-usb-approval-bridge` | 4 | 1 | — |
| `hungtranbkit/facebook-sales-radar` | 15 | 15 | — |
| `hungtranbkit/family-tree-manager` | 0 | 0 | — |
| `hungtranbkit/fb-reg` | 0 | 0 | — |
| `hungtranbkit/fb_check` | 0 | 0 | — |
| `hungtranbkit/fb_reg` | 0 | 0 | — |
| `hungtranbkit/fb_reg_new` | 0 | 0 | — |
| `hungtranbkit/jobradar` | 2 | 1 | — |
| `hungtranbkit/mesflow` | 3 | 4 | — |
| `hungtranbkit/mesflow-agent` | 0 | 0 | — |
| `hungtranbkit/mesflow-enterprise-ai` | 11 | 0 | — |
| `hungtranbkit/mesflow-esp32-kiosk` | 0 | 1 | — |
| `hungtranbkit/mesflow-kiosk-runtime-v2` | 0 | 0 | — |
| `hungtranbkit/mesflow-parseflow` | 8 | 19 | — |
| `hungtranbkit/mesflow-portfolio` | 0 | 0 | — |
| `hungtranbkit/mesflow-qa-center` | 0 | 0 | — |
| `hungtranbkit/mesflow-workspace` | 0 | 1 | — |
| `hungtranbkit/mini-factory-tycoon` | 0 | 0 | — |
| `hungtranbkit/mini-office` | 19 | 0 | — |
| `hungtranbkit/mvalab-design-reference` | 0 | 0 | — |
| `hungtranbkit/note` | 12 | 0 | — |
| `hungtranbkit/nova-voice` | 3 | 0 | — |
| `hungtranbkit/novafactory` | 12 | 6 | — |
| `hungtranbkit/novamaint-demo` | 0 | 0 | — |
| `hungtranbkit/novaretail-web` | 1 | 2 | — |
| `hungtranbkit/offline-pos` | 4 | 9 | — |
| `hungtranbkit/openclaw-ops` | 0 | 0 | — |
| `hungtranbkit/project-standards` | 0 | 0 | — |
| `hungtranbkit/projectflow-workspace-manager` | 0 | 0 | — |
| `hungtranbkit/property-intelligence-vn` | 3 | 5 | — |
| `hungtranbkit/qr-restaurant` | 0 | 0 | — |
| `hungtranbkit/quan-ly-xuong` | 0 | 1 | — |
| `hungtranbkit/rag-business-web` | 0 | 5 | — |
| `hungtranbkit/rag-engine` | 6 | 16 | — |
| `hungtranbkit/rea-mcp-research` | 1 | 0 | — |
| `hungtranbkit/repoport` | 0 | 0 | — |
| `hungtranbkit/routerflow` | 28 | 1 | — |
| `hungtranbkit/routerflow-landing` | 0 | 0 | — |
| `hungtranbkit/serverhub` | 0 | 0 | — |
| `hungtranbkit/so-tro` | 27 | 10 | — |
| `hungtranbkit/so-tro-landing` | 0 | 0 | — |
| `hungtranbkit/sodoc` | 2 | 0 | — |
| `hungtranbkit/startup-pixel` | 90 | 34 | — |
| `hungtranbkit/terminal-mcp` | 7 | 18 | — |
| `hungtranbkit/tro-ly` | 1 | 0 | No |

## Scope and sequence

1. Finish `facebook-sales-radar` cleanup first, preserving its master tracker and evidence; baseline captured after initial cleanup and concurrent PR #148 appeared.
2. `terminal-mcp` stability.
3. `so-tro`.
4. `routerflow`.
5. `rag-engine` and `chatgpt-ai-gateway`.
6. `offline-pos`.
7. `startup-pixel`.
8. Remaining repositories by risk and age, including archived repositories.

For each open item inspect acceptance criteria, comments/reviews, branch and head/base ancestry, full diff, CI, and deployment/runtime evidence. Close completed only with evidence. Use `not planned` only for superseded/duplicate/consolidated scope after preserving original links and unchecked acceptance items in an existing tracker; never represent that as completed. Close PRs only when proven superseded or already integrated; retain branches. Unique code remains active for review/fix/checks. Do not bypass protection, merge uncleared release gates, or infer authority for production, database, device, pilot, or credential changes. Preserve blocked external acceptance explicitly.

## Active-work collision and safety snapshot (2026-10-27)

Read-only session/task survey found active Routerflow APK build, FSR backlog cleanup (this task), and background work on So-Tro photo OCR; Codex RAG hybrid implementation is idle with retrieval benchmark blocked by missing `RAG_TEST_DATABASE_URL`; a Claude RAG session is waiting at a weekly-usage prompt; Routerflow mobile MVP is partial with device validation open; old Claude sessions are at modal/unknown state. No session was interrupted or resumed. Queue DB included one queued OfflinePOS task and five paused tasks; direct-task DB showed no active task. Local node resource use was material, so audit uses sequential repo work and low concurrency. Full notes are in the task checkpoint, not inferred from task receipts.

## Progress log

- 2026-10-27: full account-affiliation repository pagination completed (59 repositories); baseline saved above. FSR initial pass closed evidence-complete issues #139 and #141; consolidated 21 incomplete/smaller issues into #133 with acceptance/checklist links and closed them as `not planned`; PR #70 closed as superseded after source scope was shown already integrated. FSR after initial pass: 15 issues / 15 PRs, including concurrently opened PR #148. See issue/PR records in GitHub.
- 2026-10-27 FSR audit: #147 is an active AI Sales parent; #125/#129 were consolidated into #133 as `not planned`, with their incomplete checklists preserved in #133 and explicitly linked from #147. Corrected stale checked boxes in #133 after discovering they overstated BA-01–BA-19 acceptance; only #139/#141 are marked completed with evidence. Active unique PRs #142, #144, #145, #146, #148 remain open; #148 has synthetic 89/89 Debug and Beta tests, but Meta/inventory/device gates remain blocked. Other unique unfinished work remains: #138 awaits authorized staging and review; #111 API/scanner release gates remain; #105/#106 parser evidence/gold-label gates remain. FSR remains 15 issues / 15 PRs. Exact item evidence is in GitHub (#133 audit note; #138/#111/#148 PR descriptions/comments).
- Current remaining work: audit terminal-mcp, so-tro, routerflow, rag-engine/Gateway, offline-pos, startup-pixel, then other repos by risk and age. Append per-repo before/after counts, evidence links, actions, and blockers here as work proceeds.
