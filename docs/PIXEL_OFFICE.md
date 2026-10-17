# Văn phòng AI (Pixel Office) + ChatGPT Task Center — issue #67

`/dashboard/office` draws every live Terminal MCP session as a pixel-art
character in a top-down office, next to the tasks of the AI Gateway Task
Center ([chatgpt-ai-gateway#115](https://github.com/hungtranbkit/chatgpt-ai-gateway/issues/115)).
The goal is a dashboard that looks good **and** never lets an unfinished task
disappear because its agent went quiet.

## What is real, what is not

| Thing | Source | Notes |
|---|---|---|
| Characters | `LiveSessionMonitor` (same rows as `/dashboard/live`) | One character per live session. No invented staff. `?demo=1` shows fake data **only** under a DEMO banner. |
| Node pods | session `node_id`, `unreachable_nodes` | An unreachable node keeps a dashed red pod labelled "mất kết nối". |
| Repo / branch / last seen / log | session status, git probe, pane tail | The drawer asks for a 120-line tail of the selected session only (`expand=`). |
| ChatGPT Tasks | AI Gateway `/api/workspace/task-center/{summary,tasks}` | GET only, through `task_center_reader.py`. No copy is stored. |
| "Task trên phiên Terminal" | existing task labels / supervised / queue mapping | A view of what Terminal MCP already records, not a new registry. |
| conversations.json import | **Chưa kết nối** | The gateway reports `chatgpt_history: not_connected` and has no import endpoint. The page offers a browser-only preview with explicit consent, dedup against Task Center ids, and a disabled "Xác nhận nhập" button. Nothing is uploaded or written. |

## States: kept separate on purpose

Session (`terminal_wall`/`live_sessions` evidence) → place in the office:

| State | Where | Pose |
|---|---|---|
| RUNNING | own desk | typing, code scrolling on the monitor |
| WAITING | own desk | hand raised, amber `!` |
| ERROR | own desk | red monitor, red `!` |
| DONE (output says done) | meeting room | review `?`. This is **not** `verified_done`. |
| IDLE / COMPOSER | cafe (asleep on the sofa after 20 min) | an amber clipboard over the head and a sticky note on the desk when work is still open |
| UNKNOWN / RESTRICTED / OFFLINE | own desk | grey `?` / lock / translucent ghost |

Task Center task → `flags` (several may apply) and one `bucket` (the most urgent):
`waiting_input`, `dispatch_uncertain`, `blocked`, `needs_review`, `unknown`, `stale`
(unfinished and untouched for 12 h), `active`, `verified_done`, plus
`agent_idle_open` and `done_without_evidence`.

- `verified_done` needs the gateway state **and** evidence beyond a user note.
  Without evidence the task shows as `needs_review` + `done_without_evidence`.
- A linked session that is IDLE, DONE or OFFLINE never closes a task. It adds `agent_idle_open`.
- `dispatch_uncertain` covers two cases: gateway evidence `kind=dispatch_uncertain`, or a
  queued/running task that names a node+session that is not live.
- Tasks link to sessions only by evidence: a task id the session reports
  (label / supervised task), a shared `dt_…`/`tc_…` reference, or an explicit
  `session: <name>` in the task (unique name, or with the task's node). Titles are never matched.
- The server never filters tasks. Unfinished tasks sort first, and a truncated
  pagination is flagged "có thể thiếu task".
- During a gateway outage the last good list stays visible, marked "dữ liệu cũ".

## Configuration (opt-in)

```
TMCP_TASK_CENTER_URL=https://<gateway-host>           # http only for loopback
TMCP_TASK_CENTER_SESSION_FILE=/path/to/aigw_session   # chmod 600, one cookie value
```

Without both variables, the page shows **Chưa kết nối** and makes no call.

Security properties (tested in `tests/test_task_center_reader.py`):
- only `GET` on three whitelisted paths; no CSRF header is ever sent, so the gateway
  refuses any write even if a bug tried one;
- redirects are refused, because they would re-send the cookie elsewhere;
- the session file must be private; the token never appears in responses;
- response size is capped; errors return HTTP 200 with a `connection` state
  (`not_configured`, `unauthorized`, `error`, `forbidden`), so the office never 5xxs;
- Task Center rows are returned only to the `operator`/`owner` roles. The gateway
  scopes rows to its approved user (user isolation is verified against the real
  gateway in the staging run).

Follow-up for the gateway, as a separate PR in that repo if wanted: a dedicated read-only scoped token,
so the adapter does not need a full user session cookie.

## Art and licence survey

All art is drawn procedurally in `terminal_mcp/office_page.py` (canvas
`fillRect` pixels). No sprite sheets or third-party assets are bundled.

| Repo | Code licence | Art |
|---|---|---|
| pixel-agents-hq/pixel-agents | MIT | Characters are based on JIK-A-4 "Metro City" (itch.io), a third-party pack with its own terms. Not copied. |
| NosytLabs/agent-office | MIT | Ships its own studio art. Not copied; used for ideas only. |
| basriayaz/pixel-office | MIT | Not copied; used for ideas only (meeting room, hand-raise). |

## Accessibility and performance

- The canvas is `role=img` with a live summary, arrow keys and Enter.
  "Danh sách nhân sự" carries the same information as buttons.
- `prefers-reduced-motion` (or "Chuyển động: tắt") stops the animation loop
  entirely. Characters are placed statically and still show their state.
- Day/night theme and zoom are remembered per browser (preferences only).
- Static background on an offscreen canvas; sprite pass at ≤24 fps; the loop
  stops while the tab is hidden; one poll every 4 s, skipped while one is in flight or hidden.
- Layouts are responsive at 390 / 1366 / 1920. On phones the page uses tabs and a bottom-sheet drawer,
  with no horizontal page scroll.

## QA evidence (local staging, 2026-10-17)

The staging setup ran this branch's dashboard on loopback against the **real AI Gateway at c3f7a253**
(temp SQLite, one approved owner plus a second user whose task must never
show) and real tmux sessions on an isolated socket (`TERMINAL_MCP_TMUX_SOCKET=office-qa`).

- `images/pixel-office/live-1920-drawer.png`: 3-column layout, selected session, live log, linked Task Center task
- `images/pixel-office/live-1366.png`: laptop layout with the must-not-miss strip
- `images/pixel-office/live-390.png`, `live-390-drawer.png`, `live-390-tasks.png`: phone layout, bottom sheet, Tasks tab
- `images/pixel-office/live-1366-import-preview.png`: conversations.json consent + preview + dedup; confirm stays disabled ("Chưa kết nối")
- `images/pixel-office/demo-1920-night.png`: explicit `?demo=1`, night theme, every pose (sleeping, ghost, lock, review, error, hand raise)
- `images/pixel-office/demo-1366-reduced-motion.png`: `prefers-reduced-motion: reduce` renders a static office
