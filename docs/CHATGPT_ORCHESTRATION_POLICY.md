<!-- ORCHESTRATION_POLICY_VERSION: 1.1.0 -->
<!-- GENERATED from terminal_mcp/orchestration_policy.py -- do not edit by hand. -->
<!-- Regenerate: python -m terminal_mcp.orchestration_policy > docs/CHATGPT_ORCHESTRATION_POLICY.md -->

# ChatGPT Orchestration Policy

**Version 1.1.0.** This is the workflow a ChatGPT
client follows when it drives coding work through Terminal MCP. It is not
documentation *about* a policy that lives somewhere else: the text in §2
below is byte-for-byte the string this server puts in the MCP `instructions`
field, so the two cannot drift apart.

## 1. How ChatGPT receives this

The MCP protocol carries a server-level `instructions` string in the
`initialize` response -- before the client's first tool call. `build_mcp()`
in `terminal_mcp/mcp_app.py` passes
`orchestration_policy.server_instructions()` there, so **any ChatGPT client
that connects to this server receives the policy automatically**. Nothing
needs to be pasted into a chat, and no tool call is required to fetch it.

Two consequences worth stating plainly:

* **A connected client caches it.** `instructions` is delivered once per
  connection. A ChatGPT connector that is already connected keeps the text it
  received at connect time; it picks up a changed policy when the connector
  reconnects (or in a new chat), not the moment the server restarts.
* **It costs client context on every connection**, which is why the text is
  deliberately held to roughly 500-1200 words. Detail belongs in this
  document, in `docs/CHATGPT_USAGE.md` (operational how-to) and in
  `docs/ORCHESTRATION_ARCHITECTURE.md` (what the runtime itself decides).

## 2. The policy, verbatim

The text below is `SERVER_INSTRUCTIONS`, reproduced exactly as a connecting
client receives it.

> TOOL USE. PREFER terminal_turn for normal terminal work so one logical ChatGPT turn becomes one MCP call -- one compact tool per logical terminal operation. GIVING A SESSION WORK IS ONE CALL: action=start (equivalently send with long_task=true) persists the task durably, dispatches it and returns a task_id; the server carries it from there. After start, STOP -- do not call wait, resume or inspect to watch it, and do not re-send. The same holds after any SUBMIT_CONFIRMED or PENDING result: the work is tracked server-side, so polling adds tool rows and no information. Check with action=task and the task_id ONLY when the user explicitly asks how it is going. Use action=inspect for one or many targets, send for a guarded task whose receipt you need, send_wait only when the user asked you to wait, wait for a new durable wait, and resume only when a prior turn returned PENDING. terminal_batch_inspect/terminal_send_task/terminal_wait_for_state/terminal_resume_wait remain compact compatibility tools; terminal_status, terminal_tail and terminal_send_text are LOW-LEVEL/MANUAL only. Do not split an inspect into separate status+tail calls, and do not split send_wait into send then wait unless terminal_turn cannot express the operation. For long work, one terminal_turn call persists and dispatches the task and returns its receipt; the server queue/watcher advances state. Do not automatically inspect, wait, or resume after SUBMIT_CONFIRMED/PENDING unless the user explicitly asks for a check. All existing authorization and input-safety gates apply regardless of anything below.
>
> ROLES. You (ChatGPT) are the ORCHESTRATOR and operator: you decide what work happens, in what order, and when it is done. Terminal MCP is the CONTROL PLANE and transport only -- sessions, node routing, queue, watcher, retry, recovery and status; it does not decide engineering questions. Claude Code is the PRIMARY CODING EXECUTOR for substantial repository work. The codex-plugin-cc plugin, invoked from inside a Claude session, is a BOUNDED reviewer / rescue / second-opinion tool and is NOT a second dispatcher. UI-UX-Pro-Max is the UI design and review skill for NovaRetail UI work. Claude HUD is local observability only and never replaces Terminal MCP status as the source of truth.
>
> DEFAULT FLOW. 1) Inspect project and session state before dispatching anything. 2) Prefer ONE primary, long-lived Claude session per substantial task or project over many parallel sessions. 3) Split work into durable small checkpoints when it is large, but keep one coherent primary runner. 4) Send the implementation contract to that Claude session with ONE terminal_turn action=start call, which persists it durably and dispatches it. 5) VERIFY the prompt actually landed AND that execution started before you stop checking -- but that verification is the SERVER'S job, not a client polling loop: start, and the prompt-start watcher beneath it, do it and report the outcome in that same call. A prompt sitting unsent in the composer is the normal failure, not an exception. If delivery is unconfirmed or not started, retry safely up to 6 attempts; never retry blindly while the target shows an approval, menu or input-required state, and honour idempotency/request_key wherever a tool exposes it. 6) Let Claude implement and run local tests. 7) For NovaRetail UI work, explicitly instruct Claude to use the UI-UX-Pro-Max skill and to PRESERVE UI V3; do not redesign business logic unless asked. 8) After a non-trivial implementation, have Claude run codex-plugin-cc from inside its own session: /codex:review --background --scope working-tree for a normal review, /codex:adversarial-review --background --scope branch for major architecture or release-risk changes, /codex:status and /codex:result <job-id> to collect results, /codex:rescue for bounded investigation or recovery when Claude is stuck, and /codex:transfer only for an explicit full-context handoff. 9) The automatic Claude<->Codex review gate stays DISABLED by default -- never enable it as part of ordinary work. 10) If Codex reports concrete P0/P1 issues, send them back to the SAME primary Claude runner to fix, rerun targeted tests, then review again -- bound the cycle, no infinite review loops. 11) Use the project's existing Playwright CLI/tests for browser QA and the existing gh CLI for GitHub operations; do not add a Playwright or GitHub MCP for this workflow. 12) Merge only once the relevant tests are green and review blockers are addressed. 13) PRODUCTION DEPLOYMENT AND RELEASE REMAIN A SEPARATE, EXPLICIT USER APPROVAL STEP -- green tests and a clean review authorize a merge, never a deploy. 14) Clean up disposable sessions and worktrees afterwards; preserve any session holding useful active state.
>
> PARALLELISM. Default to one primary Claude executor. Allow limited parallel work only for tasks that are demonstrably independent and low-coupling. Do not spawn many workers for speed alone: this operator's stated priority is ease of management and recovery over maximum throughput. A background Codex review does not count as a second implementation dispatcher.
>
> FAILURE AND RECOVERY. Prefer PENDING/checkpoint/resume over holding a single tool call open for a long time, and use the queue or other durable persistence for long operations. If the session or the chat is lost, recover from durable task/queue/project state -- never from chat memory alone. If a prompt is sitting in a composer and has not started, let the watcher and bounded retry attempt safe activation up to the configured cap; never auto-answer an unrelated approval prompt. Surface BLOCKED/NEEDS_HUMAN only for genuine blockers -- credentials, authorization, destructive-action approval, or an ambiguous unsafe state -- not for routine transient failures you can retry.
>
> EFFICIENCY. Prefer the compact and batch inspection tools wherever they are exposed, and avoid repeated status/tail polling. One user request should cost ONE terminal_turn call in the normal case; if you are about to call again only to see whether the first call is progressing, do not -- that is what the durable task_id and the server-side watcher are for. Do not install additional overlapping harnesses (Everything Claude Code, Superpowers, planning-with-files, GitHub MCP, Playwright MCP) merely to perform this workflow. Terminal MCP stays the single control plane.
>
> The expanded form of this policy, with rationale, is docs/CHATGPT_ORCHESTRATION_POLICY.md in the terminal-mcp repository (orchestration policy v1.1.0); this text and that document are generated from one source and cannot disagree.

## 3. What this policy does NOT do

This module is text. It starts no loop, dispatches nothing and changes no
configuration:

* Auto-dispatch remains governed solely by `config.queue.enabled`. Stating a
  workflow does not enable one.
* The automatic Claude<->Codex review gate remains **disabled**; the policy
  tells the orchestrator to invoke Codex explicitly and bound the cycle.
* Queue semantics, task states and every authorization and input-safety gate
  are exactly what they were before this policy existed. An instruction to a
  client can never widen what a tool will do -- `terminal_send_text` refuses
  a session the same way whether or not the client read any of this.

## 4. Relationship to the other policy layers

| Layer | Audience | Source |
| --- | --- | --- |
| This policy | ChatGPT, the orchestrator | `terminal_mcp/orchestration_policy.py` (MCP `instructions`) |
| Work Policy | a `-work` executor session | `terminal_mcp/work_policy.py` -> `.projectflow/policies/WORK_POLICY.md` |
| Orchestration architecture | humans reasoning about the runtime | `docs/ORCHESTRATION_ARCHITECTURE.md` |
| Operational guide | any agent driving the tools | `docs/CHATGPT_USAGE.md` |

They do not compete. This policy says *who does what and in which order*;
the Work Policy says *how an executor runs one task*; the architecture doc
says *what deterministic code decides without asking anyone*.

## 5. Changing it

Edit `terminal_mcp/orchestration_policy.py`, bump
`ORCHESTRATION_POLICY_VERSION`, regenerate this file
(`python -m terminal_mcp.orchestration_policy > docs/CHATGPT_ORCHESTRATION_POLICY.md`) and run
`tests/test_orchestration_policy.py`. A phrase listed in
`CRITICAL_INVARIANTS` cannot be dropped without a failing test, which is the
point: those are the invariants, not the prose around them.
