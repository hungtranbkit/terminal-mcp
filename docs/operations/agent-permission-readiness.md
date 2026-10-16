# Agent readiness and tool permissions

A running CLI and visible composer prove neither tool execution nor macOS permission. Agent creation now returns `state=CREATED`, `readiness_stage=TOOL_PREFLIGHT_REQUIRED`, `readiness_status=WAITING_INPUT`, and `tool_ready=false`. `agent_process_ready` and `composer_ready` report the narrower observations. Shell readiness is unchanged. `tool_ready=false` means not verified, not a permanent denial.

An active Codex tool approval menu takes precedence over a visible composer. Startup returns `BLOCKED_PERMISSION` / `CODEX_TOOL_PERMISSION_REQUIRED`; status returns `WAITING_INPUT`; normal prompt delivery is refused. An initial-prompt preflight blocker overrides the creation receipt instead of leaving a stale READY. Initial prompts still pass through existing grants, identity checks and guarded submission after the actual agent composer settles. A merely created session still running its launcher shell cannot receive an agent task.

No code in this fix selects Allow, Allow for session, or Always allow. Terminal input grants do not grant Codex MCP-tool approval, Apple Events Automation, or Safari JavaScript permissions. Already configured owner grants remain the CLI/OS's responsibility; absent verifiable scope, stop for an owner decision. Existing startup navigation only handles known non-granting choices under current input authorization, and never dismisses a tool permission dialog using an old update menu.

Agent moves for Codex/Claude now fail before touching either node with `MOVE_REQUIRES_TOOL_PREFLIGHT`. The one-shot move cannot verify tool readiness. Create a separately named destination, verify an authorized tool there, and explicitly decide whether to stop the source. Shell moves retain their existing readiness gate.

## Unblocking affiliate-chon-tech-mac

Observed on 2026-10-16: session is on `mac17-macbook-pro`; its latest output says Safari requires **Allow JavaScript from Apple Events** in Developer settings. The agent has returned to its composer. This is an unresolved operation permission, not proof of a currently displayed approval menu.

1. The Mac owner reviews the exact `terminal.start_process` command and intended Safari/Facebook operation. If a tool approval prompt is present and the owner authorizes only this operation, choose its one-time approval option. Do not infer permission from a general Terminal MCP input grant or choose Always allow automatically.
2. If macOS requests Automation permission, the owner grants Safari access only to the actual requesting app under **System Settings → Privacy & Security → Automation**, if intended. Do not modify TCC databases or disable SIP/sandboxing.
3. If the intended workflow requires AppleScript JavaScript execution, the owner reviews and enables **Allow JavaScript from Apple Events** in Safari's Developer settings (location varies by Safari version). Do not substitute ordinary web-page “Enable JavaScript”, enable unrelated automation controls, or script the approval dialog.
4. Resume the same affiliate session only after that approval. Ask it to run a narrow, read-only probe through the same tool and caller identity used by the failed operation. Record the tool's actual success/error. A shell `echo`, CLI availability, composer visibility, an Enter receipt, or another process's Automation grant cannot prove that Safari operation works.
5. On denial, leave `BLOCKED_PERMISSION`/`WAITING_INPUT` and report the exact unmet permission. On success, continue only the approved workflow; publishing or other writes still require their own authorization.

Apple reference: https://developer.apple.com/documentation/safari-developer-tools/developer-settings

Native macOS/Linux test results and deployment evidence are recorded in the fix PR. UI fixtures test modal detection without granting permissions; they do not claim the owner's Safari grant exists.
