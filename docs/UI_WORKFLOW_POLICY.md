# UI Workflow Policy (TMCP-UI-WORKFLOW-001)

Policy version 1.0.0. Generated from `terminal_mcp/ui_workflow.py`; do not edit by hand.

## Deterministic flow

```
TASK -> project profile -> Taste -> [Image-to-Code only with a reference]
     -> implementation -> Web Design Guidelines audit
     -> fix critical/major -> Playwright verify -> fix/reverify
     -> commit -> safe deploy -> live verify
```

## Precedence (lower number wins)

| # | Layer | Authority |
| --- | --- | --- |
| 1 | `user_instruction` | what the operator asked for in this task |
| 2 | `project_ui_rules` | the project's own UI rules (profile below) |
| 3 | `design_system` | the product's existing tokens/components |
| 4 | `reference_mockup` | a supplied screenshot/mockup |
| 5 | `taste` | Taste skill art direction |
| 6 | `web_design_guidelines` | Vercel Web Design Guidelines |
| 7 | `generic_ui_skill` | UI-UX-Pro-Max / generic support |

A lower-precedence layer may add detail. It may never override a higher one.

## Audit gate

| Severity | Verdict |
| --- | --- |
| critical > 0 | `FAIL` -- does not proceed |
| major > 0 | `FIX_REQUIRED` -- fix before browser verification |
| minor > 0 | `PASS` -- fix when low risk, else report |

## Project profiles

### NovaRetail (`novaretail`)

- Viewports: 1366x768, 1920x1080, 390x844
- Checks: responsive, overflow, console, network, forms, modals, tables_lists, print
- Behaviour change allowed: False
- Rule: UI V3 ONLY -- never revive or reintroduce V2.
- Rule: Reuse the shared design tokens and components; do not fork them.
- Rule: Do not redesign business flows unless the operator asked explicitly.

### MESFlow (`mesflow`)

- Viewports: 1366x768, 1920x1080, 390x844
- Checks: responsive, overflow, console, network, dashboard, po_part_operation_views, kiosk_mobile, quantity_multisession
- Behaviour change allowed: False
- Rule: Preserve the current MESFlow UX rules; this is not a redesign.
- Rule: Do not change application behaviour -- a safe test fixture only, and only if strictly necessary.

### Generic project (`generic`)

- Viewports: 1366x768, 1920x1080, 390x844
- Checks: responsive, overflow, console, network
- Behaviour change allowed: False
- Rule: No project profile matched -- treat existing UI as authoritative and make the smallest change that satisfies the request.

## Skills

Curated and pinned. This policy never fetches a skill at runtime; a
missing skill is reported missing.

| Skill | Layer | Purpose |
| --- | --- | --- |
| `taste` | `taste` | art direction and design quality |
| `taste:image-to-code` | `reference_mockup` | turn a reference screenshot/mockup into code; CONDITIONAL -- only with a reference |
| `web-design-guidelines` | `web_design_guidelines` | post-implementation audit gate (Vercel Web Design Guidelines) |
| `frontend-design` | `generic_ui_skill` | Anthropic frontend-design skill; generic UI support |
| `ui-ux-pro-max` | `generic_ui_skill` | generic UI support, DELIBERATELY lowest precedence |
| `awesome-design-agent-skills` (catalog only) | `generic_ui_skill` | curated index only -- never bulk-loaded at runtime |

## Verification

Verification is the existing Browser Gateway compact surface and nothing
else: `browser_verify`, `browser_status`, `browser_screenshot`,
`browser_stop` via `terminal_turn`. This policy decides WHAT to verify;
it never opens a browser itself.

## How to use it

    terminal_ui_workflow_plan(task="...", repo_root="/home/dell/workspace/novaretail-web")
    terminal_ui_workflow_audit_gate(counts={"critical": 0, "major": 2})
    terminal_ui_workflow_policy()

## Declaring installed skills

The policy never fetches a skill. Record what is actually installed once, in
config.yaml, and a skill it was not told about is reported MISSING rather than
downloaded mid-task:

    ui_workflow:
      enabled: true
      installed_skills:
        - taste
        - web-design-guidelines
      # default_project: novaretail

## Relationship to the Browser Gateway

This policy decides WHAT to verify. Verification itself is unchanged and stays
on the existing compact surface -- terminal_turn browser_verify /
browser_status / browser_screenshot / browser_stop. Nothing here opens a
browser, and tests/test_ui_workflow.py asserts this feature added no
terminal_browser_* tool and no new terminal_turn action.
