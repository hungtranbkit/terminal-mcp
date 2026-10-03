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

ORCHESTRATION_POLICY_VERSION = "1.4.1"

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
    "TOOL EFFICIENCY. Terminal MCP is the CONTROL PLANE. GIVING A SESSION WORK IS DIRECT SESSION SEND. "
    "The durable task queue is retired and disabled by default. For coding work, create a fresh session when needed, then use terminal_turn action=send or action=send_wait directly on that session. "
    "Queue-producing actions start, enqueue_task, route_start, agent_start, project_start, aliases run/dispatch/enqueue, and send with long_task=true must not be used in normal operation. "
    "Use inspect only when the user explicitly asks for a status check. After direct send, avoid polling unless the user asks for a check. "
    "Read-only task history/status and cleanup remain available for historical tasks. Paperclip is a separate optional orchestration layer for project/issue assignment, heartbeats, budgets and managed workspaces; it is not a dependency of direct Terminal MCP inspection, remote execution, browser verification or break-glass recovery. When work is complete, verify it, merge to the intended branch, and clean up finished sessions/worktrees/branches."
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
    "DEFAULT FLOW. 1) Inspect project and current session state only as needed. 2) Default to one primary Claude executor for substantial repository work. "
    "3) If a new task needs a runtime and resources are available, create a fresh session/worktree/branch from the correct updated base. "
    "4) Send the implementation contract to that session with direct terminal_turn action=send or action=send_wait; do not enqueue it. "
    f"5) VERIFY the prompt actually landed AND that execution started using the guarded send result; if delivery is unconfirmed, retry safely up to {PROMPT_RETRY_CAP} attempts without creating duplicate sessions or duplicate work. "
    "6) Keep the same primary runner through implementation, targeted tests, review and routine fixes. 7) For NovaRetail UI work, explicitly instruct Claude to use the UI-UX-Pro-Max skill and to PRESERVE UI V3; do not redesign business logic unless asked. "
    "8) After a non-trivial implementation, use codex-plugin-cc from inside the same Claude session for bounded review when appropriate: /codex:review --background --scope working-tree, /codex:adversarial-review --background --scope branch for major risk, and /codex:rescue for bounded recovery. "
    "The automatic review gate stays DISABLED by default. 9) If review finds concrete P0/P1 issues, send them back to the SAME primary runner, rerun targeted tests, and bound the review/fix cycle. "
    "10) A task is not complete merely because a prompt was sent: confirm actual execution and verified results. 11) PRODUCTION DEPLOYMENT AND RELEASE REMAIN A SEPARATE, EXPLICIT USER APPROVAL STEP."
)

PARALLELISM = (
    "PARALLELISM. Default to one primary Claude executor. Allow limited parallel work only for "
    "tasks that are demonstrably independent and low-coupling. Do not spawn many workers for "
    "speed alone: this operator's stated priority is ease of management and recovery over maximum "
    "throughput. A background Codex review does not count as a second implementation dispatcher."
)

FAILURE_RECOVERY = (
    "FAILURE RECOVERY. Do not leave queued, idle or stale work behind. If a direct send fails, inspect the same session only when useful, repair the routine blocker, and retry the direct send without spawning duplicate sessions. "
    "If a session is irrecoverably stale, preserve any unmerged work, close it and create one clean replacement session. If Terminal MCP itself restarts, recover from Git/worktree state and live session state; do not create durable queue work as a fallback. "
    "Surface BLOCKED/NEEDS_HUMAN only for genuine credentials, authorization, destructive-action approval, or ambiguous unsafe states."
)

EFFICIENCY = (
    "EFFICIENCY. Prefer compact/batch inspection when inspection is actually needed, and avoid repeated status/tail polling. A normal coding dispatch is create_session once, then one direct send/send_wait to that session. "
    "Do not call start/enqueue/route_start as a shortcut. Do not keep completed sessions, worktrees or obsolete branches around. Reuse a still-valid active session for the same task; create a new one for a new task when resources allow. "
    "Terminal MCP stays the single control plane; auxiliary coding/review tools do not become a second dispatcher. "
    "TASK LABEL: when assigning or changing a session's task, pass BOTH title=\"<short summary>\" and metadata.task_summary=\"<same summary>\" on send/send_wait/supervise/create_session; this duplicate is intentional cached-client compatibility. Omit both for continuations (y, continue, approvals, small follow-ups)."
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
    # TMCP-CALLED-TOOL-SPAM-002: the one-call rule is load-bearing too -- a
    # policy that quietly loses it puts the "Called tool" wall straight back.
    "GIVING A SESSION WORK IS DIRECT SESSION SEND",
    "After direct send, avoid polling unless the user asks for a check",
    "PRODUCTION DEPLOYMENT AND RELEASE REMAIN A SEPARATE, EXPLICIT USER APPROVAL STEP",
    # Session task label: new chats only keep the live monitor accurate if
    # they are told to title task assignments.
    "pass title=\"<short summary>\" on send/send_wait",
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
