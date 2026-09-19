"""Canonical ChatGPT orchestration policy -- the workflow, as code.

The problem this solves is the same one `work_policy.py` solves one layer
down: operating rules that live only in chat history are re-taught every new
conversation, and are silently lost when a chat is reset. `work_policy.py`
fixes that for a `-work` *executor*. This module fixes it for the
*orchestrator* -- ChatGPT itself -- which never reads a repo file before it
starts issuing tool calls.

The mechanism is the MCP protocol's own server-level `instructions` field.
It is sent once, inside the `initialize` response, before the client makes
its first tool call, so every ChatGPT client that connects to this server
receives the policy automatically with no prompting, no README and no tool
call. `mcp.server.mcpserver.MCPServer` accepts it (`instructions=`) and
exposes it back as `MCPServer.instructions`; `tests/test_transports.py`
proves a real stdio and a real HTTP client both read it off the wire.

Design commitments:

* **One source of truth, no drift by construction.** `SERVER_INSTRUCTIONS`
  is the text the wire carries. `policy_document()` embeds that *same
  string* verbatim inside the human-readable doc rather than restating it,
  so the doc cannot disagree with what a client actually receives. The doc
  adds rationale around it; it never paraphrases it.
* **Concise on purpose.** These instructions occupy client context on every
  connection. The budget is roughly 500-1200 words -- enough to state the
  workflow and its invariants, not enough to become a manual. The manual is
  `docs/CHATGPT_ORCHESTRATION_POLICY.md`, which the instructions point at.
* **Tool descriptions stay short.** The policy lives at server level exactly
  so it is stated once, not appended to 280+ individual tool descriptions.
* **Describes, never enables.** This module is text. It starts no loop,
  flips no flag and dispatches nothing. Auto-dispatch stays governed by
  `config.queue.enabled` alone, and the automatic Claude<->Codex review gate
  stays off -- the policy below says so, and says so *because* nothing here
  can turn it on.
"""

from __future__ import annotations

ORCHESTRATION_POLICY_VERSION = "1.0.0"

#: Where the expanded, human-readable form lives, relative to the repo root.
POLICY_DOC_PATH = "docs/CHATGPT_ORCHESTRATION_POLICY.md"

#: The retry cap for an unconfirmed prompt delivery. Six, not "a few": it
#: matches the six-Enter verified recovery the prompt-start watcher already
#: implements (see prompt_start_watcher.py and docs/prompt-submission.md), so
#: an orchestrator following this policy and the watcher running underneath it
#: are bounded by the same number rather than two different ones.
PROMPT_RETRY_CAP = 6

# ---------------------------------------------------------------------------
# The compact tool-efficiency guidance that predates this module. Kept as its
# own constant, and kept FIRST in the wire text, because it is the part a
# client needs before its very first call -- and because several existing
# tests assert on it by phrase.
# ---------------------------------------------------------------------------
TOOL_EFFICIENCY = (
    "TOOL USE. PREFER terminal_turn for normal terminal work so one logical ChatGPT turn becomes "
    "one MCP call -- one compact tool per logical terminal operation. Use action=inspect for one "
    "or many targets, "
    "send for a guarded task (use long_task=true for durable long work), send_wait to submit and wait in one call, wait for a new durable "
    "wait, and resume only when a prior turn returned PENDING. "
    "terminal_batch_inspect/terminal_send_task/terminal_wait_for_state/terminal_resume_wait remain "
    "compact compatibility tools; terminal_status, terminal_tail and terminal_send_text are "
    "LOW-LEVEL/MANUAL only. Do not split an inspect into separate status+tail calls, and do not "
    "split send_wait into send then wait unless terminal_turn cannot express the operation. "
    "For long work, one terminal_turn call persists and dispatches the task and returns its receipt; "
    "the server queue/watcher advances state. Do not automatically inspect, wait, or resume after "
    "SUBMIT_CONFIRMED/PENDING unless the user explicitly asks for a check. All existing authorization "
    "and input-safety gates apply regardless of anything below."
)

ROLES = (
    "ROLES. You (ChatGPT) are the ORCHESTRATOR and operator: you decide what work happens, in "
    "what order, and when it is done. Terminal MCP is the CONTROL PLANE and transport only -- "
    "sessions, node routing, queue, watcher, retry, recovery and status; it does not decide "
    "engineering questions. Claude Code is the PRIMARY CODING EXECUTOR for substantial repository "
    "work. The codex-plugin-cc plugin, invoked from inside a Claude session, is a BOUNDED "
    "reviewer / rescue / second-opinion tool and is NOT a second dispatcher. UI-UX-Pro-Max is the "
    "UI design and review skill for NovaRetail UI work. Claude HUD is local observability only "
    "and never replaces Terminal MCP status as the source of truth."
)

EXECUTION_FLOW = (
    "DEFAULT FLOW. 1) Inspect project and session state before dispatching anything. "
    "2) Prefer ONE primary, long-lived Claude session per substantial task or project over many "
    "parallel sessions. 3) Split work into durable small checkpoints when it is large, but keep "
    "one coherent primary runner. 4) Send the implementation contract to that Claude session. "
    "5) VERIFY the prompt actually landed AND that execution started before you stop checking -- "
    "a prompt sitting unsent in the composer is the normal failure, not an exception. If delivery "
    f"is unconfirmed or not started, retry safely up to {PROMPT_RETRY_CAP} attempts; never retry "
    "blindly while the target shows an approval, menu or input-required state, and honour "
    "idempotency/request_key wherever a tool exposes it. 6) Let Claude implement and run local "
    "tests. 7) For NovaRetail UI work, explicitly instruct Claude to use the UI-UX-Pro-Max skill "
    "and to PRESERVE UI V3; do not redesign business logic unless asked. 8) After a non-trivial "
    "implementation, have Claude run codex-plugin-cc from inside its own session: "
    "/codex:review --background --scope working-tree for a normal review, "
    "/codex:adversarial-review --background --scope branch for major architecture or "
    "release-risk changes, /codex:status and /codex:result <job-id> to collect results, "
    "/codex:rescue for bounded investigation or recovery when Claude is stuck, and "
    "/codex:transfer only for an explicit full-context handoff. 9) The automatic Claude<->Codex "
    "review gate stays DISABLED by default -- never enable it as part of ordinary work. "
    "10) If Codex reports concrete P0/P1 issues, send them back to the SAME primary Claude runner "
    "to fix, rerun targeted tests, then review again -- bound the cycle, no infinite review loops. "
    "11) Use the project's existing Playwright CLI/tests for browser QA and the existing gh CLI "
    "for GitHub operations; do not add a Playwright or GitHub MCP for this workflow. "
    "12) Merge only once the relevant tests are green and review blockers are addressed. "
    "13) PRODUCTION DEPLOYMENT AND RELEASE REMAIN A SEPARATE, EXPLICIT USER APPROVAL STEP -- "
    "green tests and a clean review authorize a merge, never a deploy. 14) Clean up disposable "
    "sessions and worktrees afterwards; preserve any session holding useful active state."
)

PARALLELISM = (
    "PARALLELISM. Default to one primary Claude executor. Allow limited parallel work only for "
    "tasks that are demonstrably independent and low-coupling. Do not spawn many workers for "
    "speed alone: this operator's stated priority is ease of management and recovery over maximum "
    "throughput. A background Codex review does not count as a second implementation dispatcher."
)

FAILURE_RECOVERY = (
    "FAILURE AND RECOVERY. Prefer PENDING/checkpoint/resume over holding a single tool call open "
    "for a long time, and use the queue or other durable persistence for long operations. If the "
    "session or the chat is lost, recover from durable task/queue/project state -- never from chat "
    "memory alone. If a prompt is sitting in a composer and has not started, let the watcher and "
    "bounded retry attempt safe activation up to the configured cap; never auto-answer an "
    "unrelated approval prompt. Surface BLOCKED/NEEDS_HUMAN only for genuine blockers -- "
    "credentials, authorization, destructive-action approval, or an ambiguous unsafe state -- "
    "not for routine transient failures you can retry."
)

EFFICIENCY = (
    "EFFICIENCY. Prefer the compact and batch inspection tools wherever they are exposed, and "
    "avoid repeated status/tail polling. Do not install additional overlapping harnesses "
    "(Everything Claude Code, Superpowers, planning-with-files, GitHub MCP, Playwright MCP) "
    "merely to perform this workflow. Terminal MCP stays the single control plane."
)

_REFERENCE = (
    f"The expanded form of this policy, with rationale, is {POLICY_DOC_PATH} in the terminal-mcp "
    f"repository (orchestration policy v{ORCHESTRATION_POLICY_VERSION}); this text and that "
    "document are generated from one source and cannot disagree."
)

#: The exact string handed to `MCPServer(instructions=...)`, and therefore the
#: exact string a ChatGPT client receives in its `initialize` response.
SERVER_INSTRUCTIONS = "\n\n".join(
    (TOOL_EFFICIENCY, ROLES, EXECUTION_FLOW, PARALLELISM, FAILURE_RECOVERY, EFFICIENCY, _REFERENCE)
)


def server_instructions() -> str:
    """The server-level MCP `instructions` text, as sent on `initialize`."""
    return SERVER_INSTRUCTIONS


#: Phrases that MUST survive any future edit of the text above. Each one is a
#: load-bearing invariant of the workflow rather than a stylistic choice, and
#: `tests/test_orchestration_policy.py` asserts every one of them against the
#: real, built server's instructions -- so weakening one is a failing test, not
#: a silent rewrite.
CRITICAL_INVARIANTS: tuple[str, ...] = (
    "ChatGPT) are the ORCHESTRATOR",
    "Terminal MCP is the CONTROL PLANE",
    "Claude Code is the PRIMARY CODING EXECUTOR",
    "VERIFY the prompt actually landed AND that execution started",
    f"retry safely up to {PROMPT_RETRY_CAP} attempts",
    "UI-UX-Pro-Max",
    "PRESERVE UI V3",
    "/codex:review --background --scope working-tree",
    "/codex:rescue",
    "review gate stays DISABLED by default",
    "Default to one primary Claude executor",
    "PRODUCTION DEPLOYMENT AND RELEASE REMAIN A SEPARATE, EXPLICIT USER APPROVAL STEP",
)


_DOC_PREAMBLE = f"""<!-- ORCHESTRATION_POLICY_VERSION: {ORCHESTRATION_POLICY_VERSION} -->
<!-- GENERATED from terminal_mcp/orchestration_policy.py -- do not edit by hand. -->
<!-- Regenerate: python -m terminal_mcp.orchestration_policy > {POLICY_DOC_PATH} -->

# ChatGPT Orchestration Policy

**Version {ORCHESTRATION_POLICY_VERSION}.** This is the workflow a ChatGPT
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

"""

_DOC_EPILOGUE = f"""
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
(`python -m terminal_mcp.orchestration_policy > {POLICY_DOC_PATH}`) and run
`tests/test_orchestration_policy.py`. A phrase listed in
`CRITICAL_INVARIANTS` cannot be dropped without a failing test, which is the
point: those are the invariants, not the prose around them.
"""


def policy_document() -> str:
    """The full human-readable policy document, embedding the wire text.

    `SERVER_INSTRUCTIONS` is interpolated verbatim (as a blockquote-free
    fenced block) rather than restated, so `docs/CHATGPT_ORCHESTRATION_
    POLICY.md` is a rendering of the real string and not a second copy of it
    that can rot.
    """
    body = "\n".join(f"> {line}" if line else ">" for line in SERVER_INSTRUCTIONS.splitlines())
    return f"{_DOC_PREAMBLE}{body}\n{_DOC_EPILOGUE}"


if __name__ == "__main__":  # pragma: no cover -- regeneration helper
    print(policy_document(), end="")
